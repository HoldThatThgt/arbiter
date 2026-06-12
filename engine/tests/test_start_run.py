import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from arbiter_engine import rpc
from arbiter_engine.runs import async_runs


def request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":")) + "\n"


def response_for(line):
    stdin = io.StringIO(line)
    stdout = io.StringIO()
    rpc.serve(stdin, stdout)
    return json.loads(stdout.getvalue())


class StartRunTest(unittest.TestCase):
    def test_start_run_persists_completion_for_later_status_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            with chdir(tmp):
                started = response_for(
                    request(
                        "arbiter/startRun",
                        {
                            "spec": {
                                "kind": "stub",
                                "sleep_ms": 25,
                                "timeout_s": 1,
                                "result": {"overall": "passed", "passed": 1},
                            }
                        },
                    )
                )

                self.assertNotIn("error", started)
                run_id = started["result"]["run_id"]
                self.assertEqual(started["result"]["state"], "running")

                status = wait_for_terminal(run_id)
                self.assertEqual(status["state"], "completed")
                self.assertEqual(status["result"]["overall"], "passed")

                persisted = response_for(
                    request("arbiter/runStatus", {"run_id": run_id}, request_id=2)
                )
                self.assertEqual(persisted["result"]["state"], "completed")

                # The double-forked worker is a bounded job, not a daemon:
                # once the run is terminal its process must be gone (it is
                # reparented to init, so pid-gone is the assertion — no
                # zombie can linger in our process tree).
                db = Path(tmp) / ".arbiter" / "runs" / "state.sqlite"
                worker_pid = read_worker_pid(db, run_id)
                self.assertIsNotNone(worker_pid)
                wait_for_pid_gone(worker_pid)

    def test_timeout_is_recorded_as_a_failed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with chdir(tmp):
                started = response_for(
                    request(
                        "arbiter/startRun",
                        {
                            "spec": {
                                "kind": "stub",
                                "sleep_ms": 2000,
                                "timeout_s": 1,
                                "result": {"overall": "passed"},
                            }
                        },
                    )
                )

                status = wait_for_terminal(started["result"]["run_id"], timeout=3)
                self.assertEqual(status["state"], "failed")
                self.assertEqual(status["result"]["overall"], "failed")
                self.assertEqual(status["result"]["failure"], "timeout")

    def test_start_run_rejects_unknown_top_level_params(self):
        response = response_for(request("arbiter/startRun", {"spec": {}, "extra": True}))

        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["data"]["kind"], "invalid_params")
        self.assertEqual(response["error"]["data"]["bad_params"], ["extra"])

    def test_run_kind_requires_a_non_empty_recipe(self):
        for spec in (
            {"kind": "run"},
            {"kind": "run", "recipe": ""},
            {"kind": "run", "recipe": "   "},
            {"kind": "run", "recipe": None},
        ):
            with self.subTest(spec=spec):
                response = response_for(request("arbiter/startRun", {"spec": spec}))

                self.assertEqual(response["error"]["code"], -32602)
                self.assertEqual(response["error"]["data"]["kind"], "invalid_params")
                self.assertEqual(response["error"]["data"]["field"], "recipe")

    def test_run_kind_never_returns_the_default_pass_stub(self):
        # A kind=="run" spec must execute the recipe even if a stub_result
        # option is smuggled in; with no recipe book present that means the
        # run fails instead of silently "passing".
        with tempfile.TemporaryDirectory() as tmp:
            with chdir(tmp):
                started = response_for(
                    request(
                        "arbiter/startRun",
                        {
                            "spec": {
                                "kind": "run",
                                "recipe": "unit",
                                "timeout_s": 5,
                                "options": {"stub_result": {"overall": "passed"}},
                            }
                        },
                    )
                )

                self.assertNotIn("error", started)
                status = wait_for_terminal(started["result"]["run_id"], timeout=5)
                self.assertEqual(status["state"], "failed")
                self.assertEqual(status["result"]["overall"], "failed")

    def test_run_reports_frozen_test_digests_it_observed(self):
        # The worker hashes the referee's frozen test sources at compile time and
        # reports {path: sha256} so the Go side can reject a run whose compiled
        # bytes differ from the frozen registry. The digest must reflect the bytes
        # on disk when the worker ran — here, the tampered content.
        with tempfile.TemporaryDirectory() as tmp:
            recipe = Path(tmp) / ".arbiter" / "recipes.yaml"
            recipe.parent.mkdir(parents=True)
            recipe.write_text(
                """
targets:
  - id: unit
    binary: build/unit
    harness:
      kind: gtest
    test_run:
      cmd: [/bin/sh, -c, "true"]
""",
                encoding="utf-8",
            )
            test_src = Path(tmp) / "tests" / "frozen_test.cc"
            test_src.parent.mkdir(parents=True)
            body = b"TAMPERED CONTENT\n"
            test_src.write_bytes(body)
            with chdir(tmp):
                started = response_for(
                    request(
                        "arbiter/startRun",
                        {
                            "spec": {
                                "kind": "run",
                                "recipe": "unit",
                                "timeout_s": 5,
                                "frozen": ["tests/frozen_test.cc"],
                            }
                        },
                    )
                )

                self.assertNotIn("error", started)
                status = wait_for_terminal(started["result"]["run_id"], timeout=5)
                digests = status["result"].get("frozen_digests")
                self.assertEqual(
                    digests,
                    {"tests/frozen_test.cc": hashlib.sha256(body).hexdigest()},
                )

    def test_run_reports_empty_digest_for_unreadable_frozen_test(self):
        # A frozen path the worker cannot read (deleted mid-run) is reported with
        # an empty digest, never silently dropped — the Go comparison fails it.
        with tempfile.TemporaryDirectory() as tmp:
            recipe = Path(tmp) / ".arbiter" / "recipes.yaml"
            recipe.parent.mkdir(parents=True)
            recipe.write_text(
                """
targets:
  - id: unit
    binary: build/unit
    harness:
      kind: gtest
    test_run:
      cmd: [/bin/sh, -c, "true"]
""",
                encoding="utf-8",
            )
            with chdir(tmp):
                started = response_for(
                    request(
                        "arbiter/startRun",
                        {
                            "spec": {
                                "kind": "run",
                                "recipe": "unit",
                                "timeout_s": 5,
                                "frozen": ["tests/gone.cc"],
                            }
                        },
                    )
                )

                self.assertNotIn("error", started)
                status = wait_for_terminal(started["result"]["run_id"], timeout=5)
                self.assertEqual(
                    status["result"].get("frozen_digests"), {"tests/gone.cc": ""}
                )

    def test_start_run_rejects_non_string_frozen_entries(self):
        response = response_for(
            request("arbiter/startRun", {"spec": {"kind": "run", "recipe": "unit", "frozen": [7]}})
        )

        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["data"]["kind"], "invalid_params")
        self.assertEqual(response["error"]["data"]["field"], "frozen")

    def test_recipe_run_is_bounded_by_spec_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            recipe = Path(tmp) / ".arbiter" / "recipes.yaml"
            recipe.parent.mkdir(parents=True)
            recipe.write_text(
                """
targets:
  - id: unit
    binary: build/unit
    harness:
      kind: gtest
    test_run:
      cmd: [/bin/sh, -c, "sleep 5"]
""",
                encoding="utf-8",
            )
            with chdir(tmp):
                started = response_for(
                    request(
                        "arbiter/startRun",
                        {"spec": {"kind": "run", "recipe": "unit", "timeout_s": 1}},
                    )
                )

                self.assertNotIn("error", started)
                status = wait_for_terminal(started["result"]["run_id"], timeout=4)
                self.assertEqual(status["state"], "failed")
                self.assertEqual(status["result"]["failure"], "timeout")

    def test_dead_worker_is_finished_as_worker_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            with chdir(tmp):
                db = Path(tmp) / ".arbiter" / "runs" / "state.sqlite"
                async_runs._init_db(db)
                async_runs._insert_run(db, "r-dead", {"kind": "stub", "sleep_ms": 0})
                dead = subprocess.Popen(
                    [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL
                )
                dead.wait()
                async_runs._record_worker(db, "r-dead", dead.pid)

                status = response_for(
                    request("arbiter/runStatus", {"run_id": "r-dead"})
                )["result"]
                self.assertEqual(status["state"], "failed")
                self.assertEqual(status["result"]["failure"], "worker_lost")

                # The failure is persisted, not recomputed.
                again = response_for(
                    request("arbiter/runStatus", {"run_id": "r-dead"}, request_id=2)
                )["result"]
                self.assertEqual(again["state"], "failed")
                self.assertEqual(again["result"]["failure"], "worker_lost")

    def test_running_status_is_kept_while_worker_is_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            with chdir(tmp):
                db = Path(tmp) / ".arbiter" / "runs" / "state.sqlite"
                async_runs._init_db(db)
                async_runs._insert_run(db, "r-alive", {"kind": "stub", "sleep_ms": 0})
                async_runs._record_worker(db, "r-alive", os.getpid())

                status = response_for(
                    request("arbiter/runStatus", {"run_id": "r-alive"})
                )["result"]
                self.assertEqual(status["state"], "running")


def read_worker_pid(db, run_id):
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT worker_pid FROM async_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def wait_for_pid_gone(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"worker pid {pid} still alive after terminal state")


def wait_for_terminal(run_id, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = response_for(request("arbiter/runStatus", {"run_id": run_id}, request_id=9))
        result = status["result"]
        if result["state"] != "running":
            return result
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


class chdir:
    def __init__(self, path):
        self.path = Path(path)
        self.old = None

    def __enter__(self):
        self.old = Path.cwd()
        os.chdir(self.path)

    def __exit__(self, exc_type, exc, tb):
        os.chdir(self.old)


if __name__ == "__main__":
    unittest.main()

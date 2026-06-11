import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from arbiter_engine import rpc
from arbiter_engine.runs import RunManager


def wait_finished(manager, run_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = manager.run_status(run_id)
        if status["status"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


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


class StartRunWorkerTest(unittest.TestCase):
    def test_worker_finishes_and_status_survives_new_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manager = RunManager(repo)

            started = manager.start_run({"duration_ms": 10, "timeout_ms": 1000})
            status = wait_finished(RunManager(repo), started["run_id"])

            self.assertEqual(status["status"], "finished")
            self.assertEqual(status["result"]["overall"], "passed")
            self.assertEqual(status["result"]["run_id"], started["run_id"])
            self.assertTrue((repo / ".arbiter" / "runs" / "state.sqlite").exists())

    def test_worker_timeout_is_bounded_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunManager(Path(tmp))

            started_at = time.monotonic()
            started = manager.start_run({"duration_ms": 1000, "timeout_ms": 20})
            status = wait_finished(manager, started["run_id"])
            elapsed = time.monotonic() - started_at

            self.assertEqual(status["status"], "timeout")
            self.assertEqual(status["result"]["overall"], "failed")
            self.assertEqual(status["result"]["reason"], "timeout")
            self.assertLess(elapsed, 0.5)

    def test_start_run_rejects_timeout_above_protocol_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunManager(Path(tmp))

            with self.assertRaises(ValueError):
                manager.start_run({"duration_ms": 0, "timeout_ms": 3600001})

    def test_start_run_and_run_status_rpc_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                started = response_for(
                    request("arbiter/startRun", {"duration_ms": 0, "timeout_ms": 1000})
                )
                run_id = started["result"]["run_id"]

                deadline = time.monotonic() + 2
                status = None
                while time.monotonic() < deadline:
                    status = response_for(
                        request("arbiter/runStatus", {"run_id": run_id}, request_id=2)
                    )
                    if status["result"]["status"] != "running":
                        break
                    time.sleep(0.02)
            finally:
                os.chdir(old_cwd)

        self.assertIsNotNone(status)
        self.assertEqual(status["result"]["status"], "finished")
        self.assertEqual(status["result"]["result"]["overall"], "passed")


if __name__ == "__main__":
    unittest.main()

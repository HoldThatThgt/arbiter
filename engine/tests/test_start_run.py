import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from arbiter_engine import rpc


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

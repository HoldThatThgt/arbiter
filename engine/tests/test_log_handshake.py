import io
import json
import tempfile
import unittest
from pathlib import Path

from arbiter_engine import __version__, rpc
from arbiter_engine import log as engine_log


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


class LogAndHandshakeTest(unittest.TestCase):
    def test_facts_log_redacts_secret_keys_and_keeps_correlation(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = engine_log.ChannelWriter(Path(tmp), "facts")
            writer.write(
                "query",
                {"query": "callers:main", "api_token": "sk-secret-value"},
                meta={"match_id": "m1", "round": 2, "task_id": "T3"},
            )

            line = (Path(tmp) / ".arbiter" / "log" / "facts.jsonl").read_text(
                encoding="utf-8"
            )

        self.assertNotIn("sk-secret-value", line)
        event = json.loads(line)
        self.assertEqual(event["channel"], "facts")
        self.assertEqual(event["event"], "query")
        self.assertEqual(event["payload"]["query"], "callers:main")
        self.assertEqual(event["payload"]["api_token"], {"redacted": True, "length": 15})
        self.assertEqual(
            event["correlation"],
            {"match_id": "m1", "round": 2, "task_id": "T3"},
        )

    def test_runs_log_summarizes_payload_values_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = engine_log.ChannelWriter(Path(tmp), "runs")
            writer.write(
                "run_output",
                {
                    "stdout": "very noisy test output",
                    "env": {"NORMAL": "kept", "PASSWORD": "super-secret"},
                },
                meta={"run_id": "r1"},
            )

            line = (Path(tmp) / ".arbiter" / "log" / "runs.jsonl").read_text(
                encoding="utf-8"
            )

        self.assertNotIn("very noisy test output", line)
        self.assertNotIn("super-secret", line)
        event = json.loads(line)
        self.assertEqual(event["payload"]["stdout"], {"length": 22})
        self.assertEqual(event["payload"]["env"]["NORMAL"], "kept")
        self.assertEqual(event["payload"]["env"]["PASSWORD"], {"redacted": True, "length": 12})
        self.assertEqual(event["correlation"], {"run_id": "r1"})

    def test_handshake_reports_version(self):
        response = response_for(
            request("arbiter/handshake", {"expected_version": __version__})
        )

        self.assertEqual(
            response,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"engine": "arbiter-engine", "version": __version__},
            },
        )

    def test_handshake_stale_version_is_typed_error(self):
        response = response_for(request("arbiter/handshake", {"expected_version": "old"}))

        self.assertEqual(response["error"]["code"], -32000)
        self.assertEqual(response["error"]["data"]["kind"], "engine_stale")
        self.assertEqual(response["error"]["data"]["expected"], "old")
        self.assertEqual(response["error"]["data"]["found"], __version__)


if __name__ == "__main__":
    unittest.main()

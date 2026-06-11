import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from arbiter_engine import config
from arbiter_engine import rpc
from arbiter_engine.facts import view
from arbiter_engine.shared import locks


@contextmanager
def working_dir(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextmanager
def engine_env(role="QUERY", seat="player"):
    with mock.patch.dict(os.environ, {"ARBITER_ENGINE_ROLE": role, "ARBITER_ENGINE_SEAT": seat}):
        yield


def response_for(line):
    stdin = io.StringIO(line)
    stdout = io.StringIO()
    rpc.serve(stdin, stdout)
    return json.loads(stdout.getvalue())


def request(method, params=None):
    message = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":")) + "\n"


def call_tool(name, arguments):
    return request("tools/call", {"name": name, "arguments": arguments})


class FactsReconcileTest(unittest.TestCase):
    def test_first_fact_access_reconciles_lazily_under_overlay_lock(self):
        with tempfile.TemporaryDirectory() as tmp, working_dir(tmp), engine_env():
            Path("a.c").write_text("int a;\n", encoding="utf-8")
            state_path = view.overlay_state_path(Path(tmp))

            response_for(request("tools/list"))
            self.assertFalse(state_path.exists())

            with mock.patch("arbiter_engine.facts.view.locks.acquire", wraps=locks.acquire) as acquire:
                response = response_for(call_tool("search", {"query": "a", "limit": 1}))

            acquired = [call.args[1] for call in acquire.call_args_list]
            self.assertIn([locks.OVERLAY], acquired)
            self.assertTrue(state_path.exists())
            self.assertEqual(response["result"]["view_state"], "overlay")
            self.assertIsNotNone(response["result"]["overlay_id"])

    def test_non_writer_reads_published_overlay_without_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp, working_dir(tmp):
            source = Path("a.c")
            source.write_text("int a;\n", encoding="utf-8")

            with engine_env():
                first = response_for(call_tool("search", {"query": "a"}))["result"]
            source.write_text("int b;\n", encoding="utf-8")

            with engine_env(role="QUERY", seat="executor"):
                executor = response_for(call_tool("search", {"query": "a"}))["result"]
            with engine_env():
                second = response_for(call_tool("search", {"query": "a"}))["result"]

            self.assertEqual(executor["overlay_id"], first["overlay_id"])
            self.assertEqual(executor["view_state"], "overlay")
            self.assertNotEqual(second["overlay_id"], first["overlay_id"])

    def test_refresh_is_writer_only_and_ttl_knob_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp, working_dir(tmp), engine_env(role="EXEC", seat="player"):
            response = response_for(request("arbiter/refresh", {"scope": {"paths": ["src"]}}))

        self.assertEqual(response["error"]["data"]["kind"], "capability_revoked")
        with self.assertRaises(config.ConfigError):
            config.parse_config("facts:\n  overlay_ttl_seconds: 600\n")


if __name__ == "__main__":
    unittest.main()

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
from arbiter_engine.shared import pipeline


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
            # The coordinator writes its run state under the OVERLAY lock on the first writer access.
            state_path = Path(tmp) / ".arbiter" / "facts" / "run" / "incremental" / "state.json"

            response_for(request("tools/list"))
            self.assertFalse(state_path.exists())

            with mock.patch("arbiter_engine.facts.view.locks.acquire", wraps=locks.acquire) as acquire:
                response = response_for(call_tool("search", {"query": "a", "limit": 1}))

            acquired = [call.args[1] for call in acquire.call_args_list]
            self.assertIn([locks.OVERLAY], acquired)
            self.assertTrue(state_path.exists())
            content = response["result"]["structuredContent"]
            # No published snapshot -> the writer reconciles to a clean base view, not an overlay.
            self.assertEqual(content["view_state"], "base")
            self.assertIsNone(content["overlay_id"])

    def test_non_writer_does_not_reconcile_or_take_the_overlay_lock(self):
        with tempfile.TemporaryDirectory() as tmp, working_dir(tmp):
            Path("a.c").write_text("int a;\n", encoding="utf-8")

            # A non-writer (executor) reads the published view; it must never reconcile, so it
            # never takes the OVERLAY lock (the single-writer rule, ADR-0009).
            with engine_env(role="QUERY", seat="executor"):
                with mock.patch("arbiter_engine.facts.view.locks.acquire", wraps=locks.acquire) as acquire:
                    executor = response_for(call_tool("search", {"query": "a"}))["result"]["structuredContent"]

            self.assertEqual([call.args[1] for call in acquire.call_args_list], [])
            self.assertEqual(executor["view_state"], "base")
            self.assertIsNone(executor["overlay_id"])

    def test_refresh_reports_published_snapshot_id_not_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(parents=True)
            (root / "src" / "a.c").write_text("int a(void) { return 1; }\n", encoding="utf-8")
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(
                json.dumps(
                    {
                        "argv": ["clang", "-c", "src/a.c", "-o", "build/a.o"],
                        "cwd": str(root),
                        "src": "src/a.c",
                        "out": "build/a.o",
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            published = pipeline.publish_after_build(root, [journal], root / "compile_commands.json")
            fact_view = view.refresh(root, view.AccessContext(role="QUERY", seat="player"))

            self.assertTrue(published.published)
            self.assertEqual(fact_view.base_snapshot_id, published.snapshot_id)
            self.assertNotEqual(fact_view.base_snapshot_id, "current")

    def test_missing_snapshot_manifest_falls_back_to_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".arbiter" / "facts" / "snapshots" / "current").mkdir(parents=True)

            fact_view = view.read_published(root)

            self.assertEqual(fact_view.base_snapshot_id, "current")

    def test_refresh_is_writer_only_and_ttl_knob_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp, working_dir(tmp), engine_env(role="EXEC", seat="player"):
            response = response_for(request("arbiter/refresh", {"scope": {"paths": ["src"]}}))

        self.assertEqual(response["error"]["data"]["kind"], "capability_revoked")
        with self.assertRaises(config.ConfigError):
            config.parse_config("facts:\n  overlay_ttl_seconds: 600\n")


if __name__ == "__main__":
    unittest.main()

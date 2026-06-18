import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from arbiter_engine import config
from arbiter_engine import errors
from arbiter_engine import rpc
from arbiter_engine.facts import incremental
from arbiter_engine.facts import view
from arbiter_engine.facts.extractor.code._shim import InitError
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

    def test_background_index_starts_for_writer_ticks_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.c").write_text("int a;\n", encoding="utf-8")
            (root / ".arbiter").mkdir()
            (root / ".arbiter" / "config.yml").write_text(
                "facts:\n  incremental:\n    poll_interval_ms: 60\n", encoding="utf-8"
            )
            state_path = root / ".arbiter" / "facts" / "run" / "incremental" / "state.json"

            background = view.start_background_index(root, view.AccessContext(role="QUERY", seat="player"))
            try:
                self.assertTrue(background.active)
                deadline = time.time() + 2.0
                while time.time() < deadline and not state_path.exists():
                    time.sleep(0.02)
                self.assertTrue(state_path.exists())  # the daemon ticked and reconciled
            finally:
                background.stop()

            # No orphan thread survives the stop (torture invariant, like the seat children).
            self.assertFalse(
                any(t.name == "arbiter-facts-bg-index" and t.is_alive() for t in threading.enumerate())
            )

    def test_background_index_is_inactive_for_non_writer_and_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            non_writer = view.start_background_index(root, view.AccessContext(role="QUERY", seat="executor"))
            self.assertFalse(non_writer.active)
            non_writer.stop()  # safe no-op

            (root / ".arbiter").mkdir()
            (root / ".arbiter" / "config.yml").write_text(
                "facts:\n  incremental:\n    enabled: false\n", encoding="utf-8"
            )
            disabled = view.start_background_index(root, view.AccessContext(role="QUERY", seat="player"))
            self.assertFalse(disabled.active)
            disabled.stop()

    def test_refresh_is_writer_only_and_ttl_knob_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp, working_dir(tmp), engine_env(role="EXEC", seat="player"):
            response = response_for(request("arbiter/refresh", {"scope": {"paths": ["src"]}}))

        self.assertEqual(response["error"]["data"]["kind"], "capability_revoked")
        with self.assertRaises(config.ConfigError):
            config.parse_config("facts:\n  overlay_ttl_seconds: 600\n")

    def test_reconcile_extractor_config_applies_indexer_toolchain_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".arbiter").mkdir()
            (root / ".arbiter" / "config.yml").write_text(
                "facts:\n"
                "  toolchain:\n"
                "    clang: /usr/lib/llvm-16/bin/clang\n"
                "    clang_args: [--gcc-toolchain=/opt/gcc-7.3.0]\n",
                encoding="utf-8",
            )

            extractor_config = view._reconcile_extractor_config(root, 3)

        # facts.toolchain flows into the extractor's ExtractorConfig (indexer-only) ...
        self.assertEqual(extractor_config.clang_executable, "/usr/lib/llvm-16/bin/clang")
        self.assertEqual(extractor_config.clang_args, ("--gcc-toolchain=/opt/gcc-7.3.0",))
        # ... while unpinned fields and the worker count are untouched.
        self.assertIsNone(extractor_config.libclang_library_path)
        self.assertEqual(extractor_config.extractor_worker_count, 3)

    def test_synchronous_reconcile_hard_stops_when_indexer_toolchain_unusable(self):
        # Consistency with the build-tail publish: the synchronous reconcile that gates every fact
        # predicate must abort with a typed indexer_unavailable error when the indexer toolchain is
        # unusable, never silently return a stale base view (which would let adjudication proceed on
        # an out-of-date index).
        boom = InitError(
            "libclang_unavailable", "libclang library is unavailable", details={"reason": "auto_not_found"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".arbiter").mkdir()
            ctx = view.AccessContext(role="QUERY", seat="player")
            with mock.patch.object(
                incremental.IncrementalCoordinator, "reconcile_current_sources", side_effect=boom
            ):
                with self.assertRaises(errors.RPCError) as raised:
                    view.reconcile(root, ctx)

        self.assertEqual(raised.exception.data["kind"], "indexer_unavailable")
        self.assertEqual(raised.exception.data["toolchain_code"], "libclang_unavailable")

    def test_synchronous_reconcile_does_not_mislabel_non_toolchain_init_errors(self):
        # Scope guard: only toolchain failures are the hard stop. A non-toolchain InitError must not
        # be dressed up as indexer_unavailable — view.reconcile re-raises it unchanged.
        boom = InitError("malformed_compile_database", "compile database must be valid JSON")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".arbiter").mkdir()
            ctx = view.AccessContext(role="QUERY", seat="player")
            with mock.patch.object(
                incremental.IncrementalCoordinator, "reconcile_current_sources", side_effect=boom
            ):
                with self.assertRaises(InitError) as raised:
                    view.reconcile(root, ctx)

        self.assertEqual(raised.exception.code, "malformed_compile_database")


if __name__ == "__main__":
    unittest.main()

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
from arbiter_engine.config import IncrementalConfig
from arbiter_engine.facts import incremental
from arbiter_engine.facts import view
from arbiter_engine.facts.extractor.code._shim import InitError
from arbiter_engine.facts.incremental import (
    IncrementalBuildResult,
    IncrementalCoordinator,
    load_active_overlay,
    overlay_pointer_path,
)
from arbiter_engine.facts.store import FactRecord, SourceInventoryEntry, open_fact_store
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


def _stale_fact(object_id: str, source_id: str, name: str) -> FactRecord:
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=f"{name} function",
        object_source="src/alpha.c:1",
        object_profile="debug",
        payload={"fact_kind": "function", "source_id": source_id},
    )


def _stale_source(rel_path: str, *, sha256: str, size_bytes: int, mtime_ns: int) -> SourceInventoryEntry:
    return SourceInventoryEntry(
        source_id="source:a",
        rel_path=rel_path,
        source_kind="c_source",
        sha256=sha256,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        compile_command_hash="b" * 64,
        toolchain_hash="c" * 64,
        included_by=[],
        includes=[],
    )


class _OverlayExtractor:
    """A fake dirty re-extractor: the overlay it builds upserts ``fact:new`` and tombstones the
    dirty source (hiding the base ``fact:old``) — the cipher-2 overlay-view shape."""

    def extract_dirty_sources(self, dirty_sources, profile):
        return IncrementalBuildResult(
            facts=[_stale_fact("fact:new", "source:a", "New")],
            relatives=[],
            source_inventory=[_stale_source("src/alpha.c", sha256="9" * 64, size_bytes=1, mtime_ns=2)],
        )


class FactsOverlayStalenessTest(unittest.TestCase):
    """Regression guard for the wired reconcile path: the production writer builds a FRESH
    coordinator per call (view.reconcile), so the in-memory ``_active_overlay`` is always None.
    A dangling on-disk overlay pointer must never serve stale/ reverted facts as fresh (ADR-0018).
    """

    def _seed_base(self, target: Path, body: str) -> str:
        (target / "src").mkdir(parents=True, exist_ok=True)
        source_file = target / "src" / "alpha.c"
        source_file.write_text(body, encoding="utf-8")
        stat = source_file.stat()
        sha = incremental._file_sha256(source_file)
        open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
            [_stale_fact("fact:old", "source:a", "Old")],
            [],
            [_stale_source("src/alpha.c", sha256=sha, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)],
        )
        return open_fact_store(target, mode="r", log_enabled=False).stats().snapshot_id

    def _fresh_reconcile(self, target: Path) -> incremental.IncrementalStatus:
        # Mirror view.reconcile: a brand-new coordinator every call (no in-memory overlay carryover).
        return IncrementalCoordinator(target, IncrementalConfig(), extractor=_OverlayExtractor()).reconcile_current_sources()

    def test_reverting_dirty_source_clears_dangling_overlay_pointer_and_serves_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            base_body = "int old(void) { return 1; }\n"
            self._seed_base(target, base_body)
            source_file = target / "src" / "alpha.c"

            # reconcile #1: source is dirty vs the snapshot -> a fresh coordinator publishes an overlay.
            source_file.write_text("int changed(void) { return 2; }\n", encoding="utf-8")
            dirty = self._fresh_reconcile(target)
            self.assertEqual(dirty.state, "overlay")
            self.assertIsNotNone(dirty.overlay_id)
            self.assertTrue(overlay_pointer_path(target).exists())

            # reconcile #2: the edit is reverted to base content -> a *new* fresh coordinator (whose
            # in-memory _active_overlay is None) must still clear the dangling on-disk overlay.
            source_file.write_text(base_body, encoding="utf-8")
            reverted = self._fresh_reconcile(target)

            self.assertEqual(reverted.state, "ready")
            self.assertIsNone(reverted.overlay_id)
            # The pointer is gone and the patchset dir is reaped, so no reader reconstructs the overlay.
            self.assertFalse(overlay_pointer_path(target).exists())
            self.assertFalse(
                (incremental.relocation.facts_dir(target) / "run" / "incremental" / "overlays" / dirty.overlay_id).exists()
            )
            self.assertIsNone(load_active_overlay(target))

            # The rpc reader path now serves the TRUE base fact, not the stale overlay (which had
            # tombstoned fact:old and upserted fact:new).
            store = open_fact_store(target, mode="r", log_enabled=False)
            base_view = store.open_view(load_active_overlay(target))
            self.assertEqual(base_view.view_state, "base")
            self.assertIsNotNone(base_view.get_fact("fact:old"))
            self.assertIsNone(base_view.get_fact("fact:new"))
            self.assertEqual([fact.object_id for fact in base_view.search("", 10)], ["fact:old"])

    def test_reconcile_to_empty_inventory_clears_dangling_overlay_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            base_body = "int old(void) { return 1; }\n"
            self._seed_base(target, base_body)
            source_file = target / "src" / "alpha.c"
            source_file.write_text("int changed(void) { return 2; }\n", encoding="utf-8")
            dirty = self._fresh_reconcile(target)
            self.assertEqual(dirty.state, "overlay")
            self.assertTrue(overlay_pointer_path(target).exists())

            # A rebuild that publishes an empty (sourceless) snapshot hits the empty-inventory
            # "ready" return; the dangling overlay pointer must be cleared there too.
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([], [], [])
            empty = self._fresh_reconcile(target)

            self.assertEqual(empty.state, "ready")
            self.assertFalse(overlay_pointer_path(target).exists())
            self.assertIsNone(load_active_overlay(target))

    def test_stale_overlay_pointer_is_not_applied_after_base_snapshot_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            snap1 = self._seed_base(target, "int old(void) { return 1; }\n")
            source_file = target / "src" / "alpha.c"

            # Publish an overlay over snap1.
            source_file.write_text("int changed(void) { return 2; }\n", encoding="utf-8")
            dirty = self._fresh_reconcile(target)
            self.assertEqual(dirty.state, "overlay")
            self.assertTrue(overlay_pointer_path(target).exists())

            # A full rebuild publishes a NEW base snapshot carrying fact:rebuilt — without ever
            # cleaning up the overlay pointer (which is still pinned to snap1).
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_stale_fact("fact:rebuilt", "source:a", "Rebuilt")],
                [],
                [_stale_source("src/alpha.c", sha256="7" * 64, size_bytes=9, mtime_ns=3)],
            )
            snap2 = open_fact_store(target, mode="r", log_enabled=False).stats().snapshot_id
            self.assertNotEqual(snap1, snap2)
            # The pointer still dangles on disk (the writer has not reconciled yet) ...
            self.assertTrue(overlay_pointer_path(target).exists())

            # ... but load_active_overlay auto-invalidates it on the base_snapshot_id mismatch, so a
            # cross-process reader (which takes no overlay lock) never applies the stale delta: the
            # rebuilt fact is visible and the stale fact:new is gone.
            self.assertIsNone(load_active_overlay(target))
            store = open_fact_store(target, mode="r", log_enabled=False)
            rebuilt_view = store.open_view(load_active_overlay(target))
            self.assertEqual(rebuilt_view.view_state, "base")
            self.assertIsNotNone(rebuilt_view.get_fact("fact:rebuilt"))
            self.assertIsNone(rebuilt_view.get_fact("fact:new"))

    def test_reader_evidence_matches_base_data_when_overlay_pointer_is_stale_after_rebuild(self):
        # The non-writer reader does not reconcile, so state.json still advertises the snap1 overlay
        # after a rebuild to snap2. The DATA path (load_active_overlay) already serves base; the
        # EVIDENCE (read_published) must agree — never report view_state=overlay with a stale
        # overlay_id/base_snapshot_id while the results are the base view (Go referee reads both).
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            snap1 = self._seed_base(target, "int old(void) { return 1; }\n")
            source_file = target / "src" / "alpha.c"
            source_file.write_text("int changed(void) { return 2; }\n", encoding="utf-8")
            dirty = self._fresh_reconcile(target)
            self.assertEqual(dirty.state, "overlay")

            # state.json still says "overlay" (pinned to snap1); the rebuild does not touch it.
            stale_status = incremental.read_incremental_status(target)
            self.assertEqual(stale_status.state, "overlay")
            self.assertEqual(stale_status.base_snapshot_id, snap1)

            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_stale_fact("fact:rebuilt", "source:a", "Rebuilt")],
                [],
                [_stale_source("src/alpha.c", sha256="7" * 64, size_bytes=9, mtime_ns=3)],
            )
            snap2 = open_fact_store(target, mode="r", log_enabled=False).stats().snapshot_id
            self.assertNotEqual(snap1, snap2)

            evidence = view.read_published(target).evidence()
            # Evidence is downgraded to base and names the rebuilt snapshot the reader truly serves,
            # consistent with load_active_overlay() == None.
            self.assertEqual(evidence["view_state"], "base")
            self.assertIsNone(evidence["overlay_id"])
            self.assertEqual(evidence["base_snapshot_id"], snap2)
            self.assertIsNone(load_active_overlay(target))

    def test_reader_evidence_still_reports_overlay_when_pointer_is_current(self):
        # Guard the happy path: when the overlay is genuinely applicable (pointer matches the live
        # snapshot), read_published must still advertise view_state=overlay — the divergence fix
        # must not downgrade a valid overlay.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self._seed_base(target, "int old(void) { return 1; }\n")
            source_file = target / "src" / "alpha.c"
            source_file.write_text("int changed(void) { return 2; }\n", encoding="utf-8")
            dirty = self._fresh_reconcile(target)
            self.assertEqual(dirty.state, "overlay")

            evidence = view.read_published(target).evidence()
            self.assertEqual(evidence["view_state"], "overlay")
            self.assertEqual(evidence["overlay_id"], dirty.overlay_id)
            self.assertIsNotNone(load_active_overlay(target))


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cipher2.config import load_config, write_default_config
from cipher2.initializer import initialize_repository
from cipher2.incremental import IncrementalBuildResult, IncrementalCoordinator
from cipher2.storage import FactRecord, FactRelative, SourceInventoryEntry, TemporaryOverlay, open_fact_store
from tests.toolchain_helpers import write_fake_toolchain


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fact(object_id: str, source_id: str, name: str):
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=f"{name} function",
        object_source="src/alpha.c:1",
        object_profile="debug",
        payload={"fact_kind": "function", "source_id": source_id},
    )


def _source(source_id: str, rel_path: str, text: str):
    return SourceInventoryEntry(
        source_id=source_id,
        rel_path=rel_path,
        source_kind="c_source",
        sha256=_hash(text),
        size_bytes=len(text.encode("utf-8")),
        mtime_ns=1,
        compile_command_hash="b" * 64,
        toolchain_hash="c" * 64,
        included_by=[],
        includes=[],
    )


class IncrementalOverlayViewTest(unittest.TestCase):
    def test_fact_view_applies_source_tombstone_and_overlay_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)
            source = _source("source:a", "src/alpha.c", "old")
            store.replace_snapshot([_fact("fact:old", "source:a", "Old")], [], [source])
            overlay = TemporaryOverlay(
                overlay_id="overlay-test",
                view_state="overlay",
                fact_upserts=[_fact("fact:new", "source:a", "New")],
                relative_upserts=[],
                source_tombstones={"source:a"},
            )

            view = store.open_view(overlay)

            self.assertEqual(view.view_state, "overlay")
            self.assertIsNone(view.get_fact("fact:old"))
            self.assertEqual(view.get_fact("fact:new").object_name, "New")
            self.assertEqual([fact.object_id for fact in view.search("", 10)], ["fact:new"])
            self.assertEqual(view.stats().total_facts, 1)

    def test_source_tombstone_hides_base_relatives_from_dirty_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)
            caller = _fact("fact:caller", "source:a", "Caller")
            target_fact = _fact("fact:target", "source:b", "Target")
            old_call = FactRelative(
                relative_id="rel:old-call",
                from_fact_id=caller.object_id,
                to_fact_id=target_fact.object_id,
                relation_kind="direct_call",
                condition=None,
                object_profile="debug",
                evidence_source="src/alpha.c:1",
                confidence=1.0,
                payload={"source_id": "source:a"},
            )
            store.replace_snapshot([caller, target_fact], [old_call], [])
            overlay = TemporaryOverlay(
                overlay_id="overlay-test",
                view_state="overlay",
                fact_upserts=[caller],
                relative_upserts=[],
                source_tombstones={"source:a"},
            )

            view = store.open_view(overlay)

            self.assertEqual(view.relatives_for_fact(caller.object_id, direction="outgoing"), [])
            self.assertEqual(view.count_relatives_for_fact(caller.object_id, direction="outgoing"), 0)
            self.assertEqual(view.relation_search("callees:Caller", limit=10).matches, ())
            self.assertEqual(view.stats().total_relatives, 0)

    def test_overlay_relative_queries_share_visible_relative_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)
            caller = _fact("fact:caller", "source:a", "Caller")
            target_fact = _fact("fact:target", "source:b", "Target")
            base_call = FactRelative(
                relative_id="rel:base-call",
                from_fact_id=caller.object_id,
                to_fact_id=target_fact.object_id,
                relation_kind="direct_call",
                condition=None,
                object_profile="debug",
                evidence_source="src/alpha.c:1",
                confidence=1.0,
                payload={"source_id": "source:a"},
            )
            overlay_call = FactRelative(
                relative_id="rel:overlay-call",
                from_fact_id=caller.object_id,
                to_fact_id=target_fact.object_id,
                relation_kind="direct_call",
                condition=None,
                object_profile="debug",
                evidence_source="src/beta.c:1",
                confidence=1.0,
                payload={"source_id": "source:b"},
            )
            store.replace_snapshot([caller, target_fact], [base_call], [])
            view = store.open_view(
                TemporaryOverlay(
                    overlay_id="overlay-cache",
                    view_state="overlay",
                    relative_upserts=[overlay_call],
                )
            )

            scan_count = 0
            original_iter_relatives = store.iter_relatives

            def counting_iter_relatives():
                nonlocal scan_count
                scan_count += 1
                return original_iter_relatives()

            store.iter_relatives = counting_iter_relatives

            self.assertEqual(
                [
                    relative.relative_id
                    for relative in view.relatives_for_fact(
                        "fact:target",
                        direction="incoming",
                        relation_kind="direct_call",
                    )
                ],
                ["rel:base-call", "rel:overlay-call"],
            )
            self.assertEqual(
                view.count_relatives_for_fact(
                    "fact:target",
                    direction="incoming",
                    relation_kind="direct_call",
                ),
                2,
            )
            self.assertEqual(
                [
                    relative.relative_id
                    for relative in view.relatives_for_fact(
                        "fact:caller",
                        direction="outgoing",
                        relation_kind="direct_call",
                    )
                ],
                ["rel:base-call", "rel:overlay-call"],
            )
            self.assertEqual(scan_count, 1)

    def test_fact_view_relations_and_stats_use_complete_visible_fact_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)
            facts = [
                FactRecord(
                    object_id=f"fact:{index:05d}",
                    object_name=f"Fact {index}",
                    object_description="clean function",
                    object_source=f"src/unit_{index}.c:1",
                    object_profile="debug",
                    payload={"fact_kind": "function", "source_id": f"source:{index:05d}"},
                )
                for index in range(10_050)
            ]
            facts.append(_fact("fact:target", "source:target", "Target"))
            relatives = [
                FactRelative(
                    relative_id="rel:tail",
                    from_fact_id="fact:10049",
                    to_fact_id="fact:target",
                    relation_kind="direct_call",
                    condition=None,
                    object_profile="debug",
                    evidence_source="src/unit_10049.c:2",
                    confidence=1.0,
                    payload={"source_id": "source:10049"},
                )
            ]
            store.replace_snapshot(facts, relatives, [])

            view = store.open_view(TemporaryOverlay(overlay_id="overlay-empty", view_state="overlay"))

            self.assertEqual(view.stats().total_facts, 10_051)
            self.assertEqual(
                [relative.relative_id for relative in view.relatives_for_fact("fact:target", direction="incoming")],
                ["rel:tail"],
            )
            self.assertEqual(view.count_relatives_for_fact("fact:target", direction="incoming"), 1)

    def test_notify_file_changed_builds_overlay_and_records_incremental_log(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                self.dirty_sources = list(dirty_sources)
                return IncrementalBuildResult(
                    facts=[_fact("fact:new", "source:a", "New")],
                    relatives=[],
                    source_inventory=[_source("source:a", "src/alpha.c", "new")],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            source = _source("source:a", "src/alpha.c", "old")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [source],
            )
            source_file.write_text("new", encoding="utf-8")
            fake = FakeExtractor()

            coordinator = IncrementalCoordinator(target, load_config(target, observe=False), extractor=fake)
            status = coordinator.notify_file_changed(source_file)

            self.assertEqual(status.state, "overlay")
            self.assertEqual(fake.dirty_sources[0].source_id, "source:a")
            self.assertIsNone(coordinator.current_view().get_fact("fact:old"))
            self.assertEqual(coordinator.current_view().get_fact("fact:new").object_name, "New")
            run_dir = target / ".cipher" / "run" / "incremental" / "overlays" / status.overlay_id
            self.assertTrue((run_dir / "facts.upsert.jsonl").exists())
            self.assertTrue((run_dir / "facts.tombstone.jsonl").exists())
            self.assertTrue((run_dir / "relatives.upsert.jsonl").exists())
            self.assertTrue((run_dir / "relatives.tombstone.jsonl").exists())
            self.assertTrue((run_dir / "stats.json").exists())
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["fact_upsert_count"], 1)
            self.assertEqual(manifest["fact_tombstone_count"], 1)
            events = [
                event.event_name
                for event in coordinator.log.read_events(channel="incremental").events
            ]
            self.assertIn("incremental.dirty_planned", events)
            self.assertIn("incremental.overlay_published", events)

    def test_pending_view_is_visible_while_extractor_runs(self):
        class SlowExtractor:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()

            def extract_dirty_sources(self, dirty_sources, profile):
                self.started.set()
                self.release.wait(timeout=2.0)
                return IncrementalBuildResult(
                    facts=[_fact("fact:new", "source:a", "New")],
                    relatives=[],
                    source_inventory=[_source("source:a", "src/alpha.c", "new")],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            source_file.write_text("new", encoding="utf-8")
            extractor = SlowExtractor()
            coordinator = IncrementalCoordinator(target, load_config(target, observe=False), extractor=extractor)
            worker = threading.Thread(target=lambda: coordinator.notify_file_changed(source_file))

            worker.start()
            self.assertTrue(extractor.started.wait(timeout=2.0))
            pending_view = coordinator.current_view()
            self.assertEqual(pending_view.view_state, "pending")
            self.assertEqual(pending_view.get_fact("fact:old").object_name, "Old")
            self.assertEqual(pending_view._overlay.pending_task_count, 1)
            extractor.release.set()
            worker.join(timeout=2.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(coordinator.current_view().view_state, "overlay")

    def test_overlay_ttl_drops_overlay_to_base_and_records_warning(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                return IncrementalBuildResult(
                    facts=[_fact("fact:new", "source:a", "New")],
                    relatives=[],
                    source_inventory=[_source("source:a", "src/alpha.c", "new")],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            source_file.write_text("new", encoding="utf-8")
            config = load_config(target, overrides={"incremental": {"overlay_ttl_seconds": 10}}, observe=False)
            coordinator = IncrementalCoordinator(target, config, extractor=FakeExtractor())
            status = coordinator.notify_file_changed(source_file)
            self.assertEqual(status.state, "overlay")

            coordinator._overlay_guard.last_access_monotonic -= 11.0
            view = coordinator.current_view()

            self.assertEqual(view.view_state, "base")
            self.assertEqual(view.get_fact("fact:old").object_name, "Old")
            events = coordinator.log.read_events(channel="incremental").events
            drops = [event for event in events if event.event_name == "incremental.overlay_dropped"]
            self.assertEqual(drops[-1].status, "warning")
            self.assertEqual(drops[-1].payload["reason"], "ttl_expired")

    def test_base_snapshot_change_drops_overlay_before_query(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                return IncrementalBuildResult(
                    facts=[_fact("fact:new", "source:a", "New")],
                    relatives=[],
                    source_inventory=[_source("source:a", "src/alpha.c", "new")],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([_fact("fact:old", "source:a", "Old")], [], [_source("source:a", "src/alpha.c", "old")])
            source_file.write_text("new", encoding="utf-8")
            coordinator = IncrementalCoordinator(target, load_config(target, observe=False), extractor=FakeExtractor())
            coordinator.notify_file_changed(source_file)
            self.assertEqual(coordinator.current_view().view_state, "overlay")

            store.replace_snapshot([_fact("fact:rebuilt", "source:a", "Rebuilt")], [], [_source("source:a", "src/alpha.c", "rebuilt")])
            view = coordinator.current_view()

            self.assertEqual(view.view_state, "base")
            self.assertIsNotNone(view.get_fact("fact:rebuilt"))
            events = coordinator.log.read_events(channel="incremental").events
            drops = [event for event in events if event.event_name == "incremental.overlay_dropped"]
            self.assertEqual(drops[-1].status, "warning")
            self.assertEqual(drops[-1].payload["reason"], "base_snapshot_changed")

    def test_compile_command_change_drops_overlay_during_full_guard_validation(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                return IncrementalBuildResult(
                    facts=[_fact("fact:new", "source:a", "New")],
                    relatives=[],
                    source_inventory=[_source("source:a", "src/alpha.c", "new")],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            (target / "build").mkdir()
            source_file = target / "src" / "alpha.c"
            compile_db = target / "build" / "compile_commands.json"
            source_file.write_text("old", encoding="utf-8")
            compile_db.write_text(
                json.dumps(
                    [
                        {
                            "directory": str(target),
                            "file": "src/alpha.c",
                            "arguments": ["cc", "-DONE=1", "src/alpha.c"],
                        }
                    ],
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            source_file.write_text("new", encoding="utf-8")
            config = load_config(
                target,
                overrides={"paths": {"compile_database": "build/compile_commands.json"}},
                observe=False,
            )
            coordinator = IncrementalCoordinator(target, config, extractor=FakeExtractor())
            coordinator.notify_file_changed(source_file)
            self.assertEqual(coordinator.current_view().view_state, "overlay")

            compile_db.write_text(
                json.dumps(
                    [
                        {
                            "directory": str(target),
                            "file": "src/alpha.c",
                            "arguments": ["cc", "-DTWO=1", "src/alpha.c"],
                        }
                    ],
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            coordinator._drop_overlay_if_invalid(validate_runtime=True)
            view = coordinator.current_view()

            self.assertEqual(view.view_state, "base")
            self.assertEqual(view.get_fact("fact:old").object_name, "Old")
            events = coordinator.log.read_events(channel="incremental").events
            drops = [event for event in events if event.event_name == "incremental.overlay_dropped"]
            self.assertEqual(drops[-1].status, "warning")
            self.assertEqual(drops[-1].payload["reason"], "compile_command_changed")

    def test_compile_command_changed_is_planned_as_dirty_reason(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                self.dirty_sources = list(dirty_sources)
                return IncrementalBuildResult(
                    facts=[_fact("fact:new", "source:a", "New")],
                    relatives=[],
                    source_inventory=[_source("source:a", "src/alpha.c", "old")],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            extractor = FakeExtractor()
            coordinator = IncrementalCoordinator(target, load_config(target, observe=False), extractor=extractor)
            coordinator._current_compile_command_hash = lambda entry: "d" * 64
            status = coordinator.notify_file_changed(source_file)

            self.assertEqual(status.state, "overlay")
            self.assertEqual(extractor.dirty_sources[0].reason, "compile_command_changed")
            events = coordinator.log.read_events(channel="incremental").events
            dirty_planned = [event for event in events if event.event_name == "incremental.dirty_planned"]
            self.assertEqual(dirty_planned[-1].payload["reason"], "compile_command_changed")

    def test_toolchain_changed_publishes_stale_warning_without_overlay(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                raise AssertionError("toolchain_changed must not extract")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            coordinator = IncrementalCoordinator(target, load_config(target, observe=False), extractor=FakeExtractor())
            coordinator._current_toolchain_hash = lambda: "d" * 64
            status = coordinator.notify_file_changed(source_file)

            self.assertEqual(status.state, "stale")
            self.assertEqual(coordinator.current_view().view_state, "stale")
            self.assertEqual(coordinator.current_view().get_fact("fact:old").object_name, "Old")
            events = coordinator.log.read_events(channel="incremental").events
            dirty_planned = [event for event in events if event.event_name == "incremental.dirty_planned"]
            self.assertEqual(dirty_planned[-1].status, "warning")
            self.assertEqual(dirty_planned[-1].payload["reason"], "toolchain_changed")

    def test_compile_database_header_change_fans_out_to_dependent_translation_unit(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                self.dirty_sources = list(dirty_sources)
                source_id = self.dirty_sources[0].source_id
                return IncrementalBuildResult(
                    facts=[_fact("fact:fanout", source_id, "Fanout")],
                    relatives=[],
                    source_inventory=[_source(source_id, "src/main.c", "fanout")],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "dep.h"
            source = target / "src" / "main.c"
            compile_db = target / "build" / "compile_commands.json"
            header.parent.mkdir(parents=True, exist_ok=True)
            source.parent.mkdir(parents=True, exist_ok=True)
            compile_db.parent.mkdir(parents=True, exist_ok=True)
            header.write_text("#define DEP_VALUE 1\n", encoding="utf-8")
            source.write_text('#include "../include/dep.h"\nint main_value(void) { return DEP_VALUE; }\n', encoding="utf-8")
            compile_db.write_text(
                json.dumps(
                    [
                        {
                            "directory": "..",
                            "file": "src/main.c",
                            "arguments": ["cc", "-Iinclude", "src/main.c"],
                        }
                    ],
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            write_fake_toolchain(target)
            write_default_config(
                target,
                compile_database="build/compile_commands.json",
                clang_executable="bin/clang",
                gcc_executable="bin/gcc",
                observe=False,
            )
            summary = initialize_repository(target, source_roots=["src"], log_enabled=False)
            self.assertEqual(summary.source_count, 1)
            inventory = {entry.rel_path: entry for entry in open_fact_store(target, mode="r").iter_source_inventory()}
            self.assertEqual(set(inventory), {"include/dep.h", "src/main.c"})
            self.assertEqual(inventory["include/dep.h"].included_by, [inventory["src/main.c"].source_id])

            header.write_text("#define DEP_VALUE 2\n", encoding="utf-8")
            fake = FakeExtractor()
            coordinator = IncrementalCoordinator(target, load_config(target, observe=False), extractor=fake)
            status = coordinator.notify_file_changed(header)

            self.assertEqual(status.state, "overlay")
            self.assertEqual(len(fake.dirty_sources), 1)
            self.assertEqual(fake.dirty_sources[0].rel_path, "src/main.c")
            self.assertEqual(fake.dirty_sources[0].reason, "included_header_changed")
            self.assertEqual(fake.dirty_sources[0].fanout_count, 1)

    def test_standard_library_poller_observes_saved_file_and_publishes_overlay(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                return IncrementalBuildResult(
                    facts=[_fact("fact:poll", "source:a", "Polled")],
                    relatives=[],
                    source_inventory=[_source("source:a", "src/alpha.c", "polled")],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            config = load_config(
                target,
                overrides={"incremental": {"poll_interval_ms": 100, "debounce_ms": 50}},
                observe=False,
            )
            coordinator = IncrementalCoordinator(target, config, extractor=FakeExtractor())

            try:
                coordinator.start()
                source_file.write_text("polled", encoding="utf-8")
                deadline = time.time() + 2.0
                while time.time() < deadline and coordinator.current_view().get_fact("fact:poll") is None:
                    time.sleep(0.05)
            finally:
                coordinator.stop()

            self.assertEqual(coordinator.current_view().view_state, "base")
            events = [event.event_name for event in coordinator.log.read_events(channel="incremental").events]
            self.assertIn("incremental.poll_started", events)
            self.assertIn("incremental.overlay_published", events)

    def test_overlay_endpoint_orphan_keeps_base_view_and_records_error(self):
        class BadExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                return IncrementalBuildResult(
                    facts=[_fact("fact:new", "source:a", "New")],
                    relatives=[
                        FactRelative(
                            relative_id="rel:orphan",
                            from_fact_id="fact:new",
                            to_fact_id="fact:missing",
                            relation_kind="direct_call",
                            condition=None,
                            object_profile="debug",
                            evidence_source="src/alpha.c:1",
                            confidence=1.0,
                            payload={"source_id": "source:a"},
                        )
                    ],
                    source_inventory=[],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            source_file.write_text("new", encoding="utf-8")

            coordinator = IncrementalCoordinator(target, load_config(target, observe=False), extractor=BadExtractor())
            status = coordinator.notify_file_changed(source_file)

            self.assertEqual(status.state, "error")
            self.assertEqual(status.latest_error_code, "overlay_endpoint_orphan")
            self.assertEqual(coordinator.current_view().view_state, "base")
            self.assertIsNotNone(coordinator.current_view().get_fact("fact:old"))

    def test_missing_changed_source_builds_tombstone_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            source_file.unlink()

            coordinator = IncrementalCoordinator(target, load_config(target, observe=False), extractor=None)
            status = coordinator.notify_file_changed(source_file)

            self.assertEqual(status.state, "overlay")
            self.assertIsNone(coordinator.current_view().get_fact("fact:old"))
            events = coordinator.log.read_events(channel="incremental").events
            dirty_planned = [event for event in events if event.event_name == "incremental.dirty_planned"]
            self.assertEqual(dirty_planned[-1].payload["reason"], "missing")


if __name__ == "__main__":
    unittest.main()

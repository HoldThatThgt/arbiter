"""Migrated from cipher-2 tests/test_incremental_overlay_view.py (M4 Phase 2).

Adaptations: imports point at arbiter_engine.facts.{store,incremental}; the config comes
from the c2 load_config shim (IncrementalConfig); the log is arbiter's real jsonl sink. The
header-fanout test builds its inventory directly via the store (instead of cipher-2's
initialize_repository, which the extractor/initializer tests cover) so it stays a focused,
hermetic test of the coordinator's fanout planning.
"""

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from arbiter_engine.facts.incremental import IncrementalBuildResult, IncrementalCoordinator
from arbiter_engine.facts.store import FactRecord, FactRelative, SourceInventoryEntry, TemporaryOverlay, open_fact_store

from c2.incremental_support import load_config


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


def _source(source_id: str, rel_path: str, text: str, *, source_kind: str = "c_source", included_by=None):
    return SourceInventoryEntry(
        source_id=source_id,
        rel_path=rel_path,
        source_kind=source_kind,
        sha256=_hash(text),
        size_bytes=len(text.encode("utf-8")),
        mtime_ns=1,
        compile_command_hash="b" * 64,
        toolchain_hash="c" * 64,
        included_by=list(included_by or []),
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
            run_dir = target / ".arbiter" / "facts" / "run" / "incremental" / "overlays" / status.overlay_id
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

    def test_included_header_change_fans_out_to_dependent_translation_unit(self):
        # The coordinator's header fanout is the unit under test; the inventory (with
        # included_by) is built directly via the store so this stays hermetic and decoupled
        # from the real extractor's inclusion-closure handling (covered by the extractor tests).
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
            header.parent.mkdir(parents=True, exist_ok=True)
            source.parent.mkdir(parents=True, exist_ok=True)
            header.write_text("#define DEP_VALUE 1\n", encoding="utf-8")
            source.write_text('#include "../include/dep.h"\nint main_value(void) { return DEP_VALUE; }\n', encoding="utf-8")
            main_entry = _source("source:main", "src/main.c", source.read_text(encoding="utf-8"))
            header_entry = _source(
                "source:dep",
                "include/dep.h",
                header.read_text(encoding="utf-8"),
                source_kind="header",
                included_by=["source:main"],
            )
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:main", "source:main", "MainValue")],
                [],
                [main_entry, header_entry],
            )

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

    def test_missing_changed_source_reports_error_without_traceback(self):
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

            self.assertEqual(status.state, "error")
            self.assertEqual(status.latest_error_code, "source_unreadable")
            events = coordinator.log.read_events(channel="incremental").events
            self.assertEqual(events[-1].error_code, "source_unreadable")


if __name__ == "__main__":
    unittest.main()

"""Age-based overlay garbage collection (ADR-0018, user-guide §9).

`facts.incremental.overlay_ttl_seconds` reaps a published overlay once its manifest
`created_at` ages past the TTL; `0` means "never GC". These tests drive the real
`IncrementalCoordinator` publish path (a fake extractor + `notify_file_changed`), then
simulate age by backdating the manifest `created_at` so the reap is deterministic — no
real sleeps, no background thread.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbiter_engine.config import IncrementalConfig
from arbiter_engine.facts import relocation
from arbiter_engine.facts.incremental import (
    IncrementalBuildResult,
    IncrementalCoordinator,
    load_active_overlay,
    overlay_pointer_path,
)
from arbiter_engine.facts.store import FactRecord, SourceInventoryEntry, open_fact_store


def _fact(object_id: str, source_id: str, name: str) -> FactRecord:
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=f"{name} function",
        object_source="src/alpha.c:1",
        object_profile="debug",
        payload={"fact_kind": "function", "source_id": source_id},
    )


def _source(source_id: str, rel_path: str, text: str) -> SourceInventoryEntry:
    return SourceInventoryEntry(
        source_id=source_id,
        rel_path=rel_path,
        source_kind="c_source",
        sha256="0" * 64,
        size_bytes=len(text.encode("utf-8")),
        mtime_ns=1,
        compile_command_hash="b" * 64,
        toolchain_hash="c" * 64,
        included_by=[],
        includes=[],
    )


class _FakeExtractor:
    def extract_dirty_sources(self, dirty_sources, profile):
        return IncrementalBuildResult(
            facts=[_fact("fact:new", "source:a", "New")],
            relatives=[],
            source_inventory=[_source("source:a", "src/alpha.c", "new")],
        )


def _publish_overlay(target: Path, *, overlay_ttl_seconds: int) -> IncrementalCoordinator:
    """Build a published overlay through the production coordinator path."""
    (target / "src").mkdir(parents=True, exist_ok=True)
    source_file = target / "src" / "alpha.c"
    source_file.write_text("old", encoding="utf-8")
    open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
        [_fact("fact:old", "source:a", "Old")],
        [],
        [_source("source:a", "src/alpha.c", "old")],
    )
    source_file.write_text("new", encoding="utf-8")

    config = replace(IncrementalConfig(), overlay_ttl_seconds=overlay_ttl_seconds)
    coordinator = IncrementalCoordinator(target, config, extractor=_FakeExtractor())
    status = coordinator.notify_file_changed(source_file)
    assert status.state == "overlay", status.state
    return coordinator


def _overlay_dir(target: Path, overlay_id: str) -> Path:
    return relocation.facts_dir(target) / "run" / "incremental" / "overlays" / overlay_id


def _backdate_manifest(target: Path, overlay_id: str, *, seconds_ago: float) -> None:
    manifest = _overlay_dir(target, overlay_id) / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    backdated = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    data["created_at"] = backdated.isoformat().replace("+00:00", "Z")
    manifest.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


class OverlayGarbageCollectionTest(unittest.TestCase):
    def test_aged_overlay_is_reaped_past_ttl_and_view_falls_back_to_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            coordinator = _publish_overlay(target, overlay_ttl_seconds=600)
            overlay_id = coordinator.status().overlay_id
            self.assertIsNotNone(overlay_id)
            overlay_dir = _overlay_dir(target, overlay_id)
            self.assertTrue(overlay_dir.is_dir())
            self.assertEqual(coordinator.current_view().view_state, "overlay")

            # Simulate the overlay aging well past the 600s TTL.
            _backdate_manifest(target, overlay_id, seconds_ago=601)
            coordinator._gc_aged_overlay()

            # The overlay is dropped: its files are gone, the pointer is cleared, and any
            # reader (the cwd-bound rpc path) now reconstructs nothing — the view is base.
            self.assertEqual(coordinator.current_view().view_state, "base")
            self.assertFalse(overlay_dir.exists())
            self.assertFalse(overlay_pointer_path(target).exists())
            self.assertIsNone(load_active_overlay(target))
            self.assertEqual(coordinator.status().state, "ready")
            self.assertIsNone(coordinator.status().overlay_id)
            events = [e.event_name for e in coordinator.log.read_events(channel="incremental").events]
            self.assertIn("incremental.overlay_dropped", events)

    def test_overlay_within_ttl_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            coordinator = _publish_overlay(target, overlay_ttl_seconds=600)
            overlay_id = coordinator.status().overlay_id

            # Aged, but still inside the TTL window — must survive.
            _backdate_manifest(target, overlay_id, seconds_ago=120)
            coordinator._gc_aged_overlay()

            self.assertEqual(coordinator.current_view().view_state, "overlay")
            self.assertTrue(_overlay_dir(target, overlay_id).is_dir())
            self.assertEqual(coordinator.status().overlay_id, overlay_id)

    def test_ttl_zero_never_gcs_an_aged_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            coordinator = _publish_overlay(target, overlay_ttl_seconds=0)
            overlay_id = coordinator.status().overlay_id

            # Far beyond any plausible TTL, yet 0 means "never GC" (user-guide §9).
            _backdate_manifest(target, overlay_id, seconds_ago=10_000_000)
            coordinator._gc_aged_overlay()

            self.assertEqual(coordinator.current_view().view_state, "overlay")
            self.assertTrue(_overlay_dir(target, overlay_id).is_dir())
            self.assertIsNotNone(load_active_overlay(target))
            self.assertEqual(coordinator.status().overlay_id, overlay_id)

    def test_gc_is_a_safe_noop_when_no_overlay_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            config = replace(IncrementalConfig(), overlay_ttl_seconds=1)
            coordinator = IncrementalCoordinator(target, config, extractor=_FakeExtractor())

            coordinator._gc_aged_overlay()  # no active overlay -> nothing to reap

            self.assertEqual(coordinator.current_view().view_state, "base")

    def test_poll_loop_reaps_aged_overlay_on_next_tick(self):
        # End-to-end through the started poll thread: the GC hook fires from _poll_loop,
        # not just a direct _gc_aged_overlay call.
        import time

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir(parents=True, exist_ok=True)
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old")],
                [],
                [_source("source:a", "src/alpha.c", "old")],
            )
            source_file.write_text("new", encoding="utf-8")
            config = replace(
                IncrementalConfig(),
                poll_interval_ms=30,
                debounce_ms=10,
                overlay_ttl_seconds=1,
            )
            coordinator = IncrementalCoordinator(target, config, extractor=_FakeExtractor())
            status = coordinator.notify_file_changed(source_file)
            overlay_id = status.overlay_id
            # Revert the source so the poll thread re-observes base and does not rebuild a
            # fresh overlay, then backdate the existing overlay past the TTL.
            source_file.write_text("old", encoding="utf-8")
            _backdate_manifest(target, overlay_id, seconds_ago=5)

            try:
                coordinator.start()
                deadline = time.time() + 2.0
                while time.time() < deadline and coordinator.current_view().view_state != "base":
                    time.sleep(0.02)
            finally:
                coordinator.stop()

            self.assertFalse(_overlay_dir(target, overlay_id).exists())


if __name__ == "__main__":
    unittest.main()

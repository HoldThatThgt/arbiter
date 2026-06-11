import json
import tempfile
import unittest
from pathlib import Path

from cipher2.incremental import IncrementalStatus
from cipher2.tools.log import LogEvent, open_log
from cipher2.tools.views import build_overview


class IncrementalObservabilityTest(unittest.TestCase):
    def test_incremental_log_events_are_visible_in_views_incremental_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(
                LogEvent(
                    event_name="incremental.overlay_published",
                    channel="incremental",
                    counts={"overlay_fact_count": 2, "overlay_relative_count": 3},
                    payload={
                        "base_snapshot_id": "sha256-base",
                        "overlay_id": "overlay-1",
                        "view_state": "overlay",
                        "publish_latency_ms": 1.5,
                    },
                )
            )

            overview = build_overview(target, include_sections=["incremental", "log"])

            self.assertEqual(overview.incremental.state, "overlay")
            self.assertEqual(overview.incremental.base_snapshot_id, "sha256-base")
            self.assertEqual(overview.incremental.active_overlay_id, "overlay-1")
            self.assertEqual(overview.incremental.overlay_fact_count, 2)
            self.assertEqual(overview.incremental.overlay_relative_count, 3)
            self.assertEqual(overview.incremental.last_publish_latency_ms, 1.5)
            self.assertIn("incremental", overview.log.events_by_channel)

    def test_incremental_view_prefers_state_file_for_stale_pending_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            state = IncrementalStatus(
                "stale",
                "sha256-base",
                dirty_source_count=2,
                pending_task_count=0,
                stale_source_count=2,
                latest_error_code="toolchain_changed",
            )
            state_path = target / ".cipher" / "run" / "incremental" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state.to_json(), sort_keys=True) + "\n", encoding="utf-8")
            open_log(target).write_event(
                LogEvent(
                    event_name="incremental.dirty_planned",
                    channel="incremental",
                    status="warning",
                    counts={"dirty_source_count": 2},
                    payload={"reason": "toolchain_changed", "base_snapshot_id": "sha256-base"},
                )
            )

            overview = build_overview(target, include_sections=["incremental", "log"])

            self.assertEqual(overview.incremental.state, "stale")
            self.assertEqual(overview.incremental.dirty_source_count, 2)
            self.assertEqual(overview.incremental.stale_source_count, 2)
            self.assertEqual(overview.incremental.latest_error_code, "toolchain_changed")


if __name__ == "__main__":
    unittest.main()

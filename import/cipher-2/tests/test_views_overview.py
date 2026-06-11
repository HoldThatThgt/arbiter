import re
import tempfile
import unittest
from pathlib import Path

from cipher2.storage import FactRecord, open_fact_store
from cipher2.tools.log import LogEvent, open_log
from cipher2.tools.views import ToolsOverviewModel, ViewBuildError, build_overview


def _fact():
    return FactRecord(
        object_id="fact:one",
        object_name="One",
        object_description="Overview input",
        object_source="src/one.py:1",
        object_profile="debug",
        payload={"fact_kind": "function"},
    )


class ViewsOverviewTest(unittest.TestCase):
    def test_include_sections_empty_returns_empty_without_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            overview = build_overview(Path(tmp), include_sections=[])

            self.assertIsInstance(overview, ToolsOverviewModel)
            self.assertEqual(overview.state, "empty")
            self.assertIsNone(overview.storage)
            self.assertIsNone(overview.log)
            self.assertEqual(overview.errors, [])
            self.assertRegex(overview.generated_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

    def test_default_overview_merges_storage_and_log_ready_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w").replace_facts([_fact()])
            open_log(target).write_event(LogEvent(event_name="mcp.search", channel="mcp", summary="queried"))

            overview = build_overview(target)

            self.assertEqual(overview.state, "ready")
            self.assertEqual(overview.storage.state, "ready")
            self.assertEqual(overview.log.state, "ready")
            self.assertEqual(overview.errors, [])

    def test_section_failure_does_not_drop_other_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            snapshot_id = (target / ".cipher" / "snapshots" / "current").read_text(encoding="utf-8")
            (target / ".cipher" / "snapshots" / snapshot_id / "stats.json").write_text("{}", encoding="utf-8")
            open_log(target).write_event(LogEvent(event_name="mcp.search", channel="mcp", summary="queried"))

            overview = build_overview(target)

            self.assertEqual(overview.state, "error")
            self.assertIsNone(overview.storage)
            self.assertEqual(overview.log.state, "ready")
            self.assertEqual(
                [(error.section, error.code) for error in overview.errors],
                [("storage", "storage_unreadable")],
            )

    def test_invalid_request_returns_synthetic_errors(self):
        cases = [
            ({"include_sections": ["bad"]}, "invalid_section"),
            ({"include_sections": [""]}, "invalid_section"),
            ({"top_n": 0}, "invalid_top_n"),
            ({"top_n": 51}, "invalid_top_n"),
            ({"since": ""}, "invalid_time_window_format"),
            ({"since": "2026-05-25T10:00:00Z"}, "invalid_time_window_format"),
            (
                {
                    "since": "2026-05-25T10:01:00.000000Z",
                    "until": "2026-05-25T10:00:00.000000Z",
                },
                "invalid_time_window",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for kwargs, code in cases:
                with self.subTest(kwargs=kwargs):
                    overview = build_overview(target, **kwargs)
                    self.assertEqual(overview.state, "error")
                    self.assertIsInstance(overview.errors[0], ViewBuildError)
                    self.assertEqual(overview.errors[0].code, code)
                    self.assertEqual(overview.errors[0].section, "*")


if __name__ == "__main__":
    unittest.main()

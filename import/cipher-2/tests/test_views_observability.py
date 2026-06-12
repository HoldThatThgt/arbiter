import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cipher2.storage import FactRecord, open_fact_store
from cipher2.tools.log import LogError, open_log
from cipher2.tools.views import build_overview


def _fact():
    return FactRecord(
        object_id="fact:one",
        object_name="One",
        object_description="Views observability input",
        object_source="src/one.py:1",
        object_profile="debug",
        payload={"fact_kind": "function"},
    )


class ViewsObservabilityTest(unittest.TestCase):
    def test_successful_build_writes_views_build_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])

            overview = build_overview(target, include_sections=["storage", "log"])

            events = open_log(target).read_events(channel="views").events
            build = [event for event in events if event.event_name == "views.build"][-1]
            self.assertEqual(build.status, "ok")
            self.assertEqual(build.payload["state"], overview.state)
            self.assertEqual(build.payload["section_count"], 2)
            self.assertGreaterEqual(build.duration_ms, 0)

    def test_section_error_writes_views_section_error_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            snapshot_id = (target / ".cipher" / "snapshots" / "current").read_text(encoding="utf-8")
            (target / ".cipher" / "snapshots" / snapshot_id / "stats.json").write_text("{}", encoding="utf-8")

            build_overview(target, include_sections=["storage"])

            events = open_log(target).read_events(channel="views").events
            section_error = [event for event in events if event.event_name == "views.section_error"][-1]
            self.assertEqual(section_error.status, "error")
            self.assertEqual(section_error.error_code, "storage_unreadable")
            self.assertEqual(section_error.payload["section"], "storage")

    def test_storage_log_summary_failure_adds_storage_log_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])

            real_open_log = open_log

            class FakeLog:
                def __init__(self, target_repo):
                    self._real = real_open_log(target_repo)

                def summarize(self, *args, **kwargs):
                    if kwargs.get("channel") == "storage":
                        raise LogError("log_read_failed", "storage log unreadable")
                    return self._real.summarize(*args, **kwargs)

                def write_event(self, event, **kwargs):
                    return self._real.write_event(event, **kwargs)

            with patch("cipher2.tools.views.open_log", side_effect=lambda target_repo: FakeLog(target_repo)):
                overview = build_overview(target, include_sections=["storage"])

            self.assertEqual(overview.state, "warning")
            self.assertEqual(overview.storage.state, "warning")
            self.assertEqual([(error.section, error.code) for error in overview.errors], [("storage.log", "log_unreadable")])
            events = open_log(target).read_events(channel="views").events
            self.assertEqual(
                [(event.event_name, event.error_code) for event in events if event.event_name == "views.section_error"],
                [("views.section_error", "log_unreadable")],
            )


if __name__ == "__main__":
    unittest.main()

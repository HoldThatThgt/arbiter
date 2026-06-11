import tempfile
import unittest
from pathlib import Path

from cipher2.tools.log import LogEvent, LogEventDigest, LogSummary, open_log


class LogSummaryForViewsTest(unittest.TestCase):
    def test_summary_exposes_view_model_inputs_without_read_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="storage.search",
                    channel="storage",
                    status="warning",
                    duration_ms=12.0,
                    summary=None,
                    error_code=None,
                    counts={"matched_count": 2},
                    payload={"query_kind": "substring", "limit": 20},
                )
            )

            summary = log.summarize(channel="storage")

            self.assertIsInstance(summary, LogSummary)
            self.assertEqual(summary.total_events, 1)
            self.assertEqual(summary.events_by_status["warning"], 1)
            self.assertEqual(summary.events_by_name["storage.search"], 1)
            self.assertEqual(summary.redaction_summary, {
                "dropped_field_count": summary.dropped_field_count,
                "truncated_field_count": summary.truncated_field_count,
            })
            self.assertIsInstance(summary.recent_events[0], LogEventDigest)
            self.assertIsNone(summary.recent_events[0].summary)
            self.assertIn(("query_kind", "substring"), summary.recent_events[0].fields)
            self.assertIn(("limit", "20"), summary.recent_events[0].fields)

    def test_empty_log_summary_supports_empty_view_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = open_log(Path(tmp)).summarize(channel="storage")

            self.assertEqual(summary.total_events, 0)
            self.assertEqual(summary.bytes_on_disk, 0)
            self.assertEqual(summary.recent_events, [])
            self.assertEqual(summary.slow_events, [])


if __name__ == "__main__":
    unittest.main()

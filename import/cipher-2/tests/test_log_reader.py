import tempfile
import unittest
from pathlib import Path

from cipher2.tools.log import LogEvent, open_log


class LogReaderTest(unittest.TestCase):
    def test_read_events_filters_channel_limit_and_time_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="storage.write",
                    channel="storage",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    summary="first",
                )
            )
            log.write_event(
                LogEvent(
                    event_name="mcp.search",
                    channel="mcp",
                    timestamp="2026-05-25T10:01:00.000000Z",
                    summary="second",
                )
            )
            log.write_event(
                LogEvent(
                    event_name="storage.search",
                    channel="storage",
                    timestamp="2026-05-25T10:02:00.000000Z",
                    summary="third",
                )
            )

            result = log.read_events(
                channel="storage",
                since="2026-05-25T10:01:00.000000Z",
                until="2026-05-25T10:03:00.000000Z",
                limit=1,
            )

            self.assertEqual([event.summary for event in result.events], ["third"])
            self.assertEqual(result.issues, [])

    def test_malformed_and_oversized_lines_are_reported_without_failing_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(LogEvent(event_name="storage.write", channel="storage", summary="ok"))
            path = target / ".cipher" / "log" / "storage.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{bad json}\n")
                handle.write("x" * (65 * 1024) + "\n")

            result = log.read_events(channel="storage")

            self.assertEqual([event.summary for event in result.events], ["ok"])
            self.assertEqual([issue.error_code for issue in result.issues], ["malformed_json", "oversized_line"])

    def test_limit_zero_returns_empty_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(LogEvent(event_name="storage.write", channel="storage", summary="ok"))

            result = log.read_events(channel="storage", limit=0)

            self.assertEqual(result.events, [])
            self.assertEqual(result.issues, [])

    def test_v1_and_v2_schema_rows_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(LogEvent(event_name="storage.write", channel="storage", schema_version=1, summary="v1"))
            log.write_event(LogEvent(event_name="storage.search", channel="storage", summary="v2"))

            result = log.read_events(channel="storage")
            summary = log.summarize(channel="storage")

            self.assertEqual([event.summary for event in result.events], ["v1", "v2"])
            self.assertEqual(result.issues, [])
            self.assertEqual(summary.total_events, 2)
            self.assertEqual(summary.malformed_lines, 0)

    def test_invalid_schema_rows_are_reported_and_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(LogEvent(event_name="storage.write", channel="storage", summary="ok"))
            path = target / ".cipher" / "log" / "storage.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"schema_version":3,"event_name":"storage.write","timestamp":"2026-05-25T10:00:00.000000Z","status":"ok","channel":"storage","counts":{},"payload":{}}\n')

            result = log.read_events(channel="storage")

            self.assertEqual([event.summary for event in result.events], ["ok"])
            self.assertEqual([issue.error_code for issue in result.issues], ["invalid_schema"])


if __name__ == "__main__":
    unittest.main()

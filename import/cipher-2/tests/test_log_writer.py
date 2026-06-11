import json
import os
import tempfile
import unittest
from pathlib import Path

from cipher2.tools.log import LogError, LogEvent, open_log


class LogWriterTest(unittest.TestCase):
    def test_append_jsonl_redacts_and_truncates_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)

            result = log.write_event(
                LogEvent(
                    event_name="storage.write",
                    channel="storage",
                    payload={
                        "api_key": "secret-value",
                        "message": "x" * 800,
                        "items": list(range(60)),
                    },
                    counts={"fact_count": 3},
                )
            )

            self.assertEqual(result.path, target / ".cipher" / "log" / "storage.jsonl")
            self.assertGreater(result.bytes_written, 0)
            line = result.path.read_text(encoding="utf-8").splitlines()[0]
            row = json.loads(line)
            self.assertEqual(row["payload"]["api_key"], "[REDACTED]")
            self.assertIn("[TRUNCATED]", row["payload"]["message"])
            self.assertEqual(len(row["payload"]["items"]), 50)

    def test_observe_batch_writes_to_original_channel_without_recursion(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)

            log.observe_batch("storage", {"write_count": 2})

            storage_rows = (target / ".cipher" / "log" / "storage.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event_name":"storage.batch_summary"', storage_rows)
            self.assertFalse((target / ".cipher" / "log" / "log.jsonl").exists())

    def test_read_and_summary_emit_log_channel_observability_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(LogEvent(event_name="storage.write", channel="storage"))

            log.read_events(channel="storage")
            log.summarize(channel="storage")
            observed = log.read_events(channel="log").events

            event_names = [event.event_name for event in observed]
            self.assertIn("log.read", event_names)
            self.assertIn("log.summary", event_names)
            summary_event = next(event for event in observed if event.event_name == "log.summary")
            self.assertEqual(summary_event.channel, "log")
            self.assertEqual(summary_event.payload["channel"], "storage")
            self.assertEqual(summary_event.counts["event_count"], 1)

    def test_write_failure_increments_dropped_count_and_reports_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "log").write_text("not a directory", encoding="utf-8")
            log = open_log(target)

            with self.assertRaises(LogError) as first:
                log.write_event(LogEvent(event_name="storage.write", channel="storage"))
            with self.assertRaises(LogError):
                log.write_event(LogEvent(event_name="storage.write", channel="storage"))

            self.assertEqual(first.exception.code, "log_write_failed")
            self.assertEqual(log.dropped_event_count, 2)
            self.assertTrue(log.stderr_reported)

    def test_too_many_channels_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))

            for index in range(32):
                log.write_event(LogEvent(event_name="storage.write", channel=f"chan{index}"))

            with self.assertRaises(LogError) as caught:
                log.write_event(LogEvent(event_name="storage.write", channel="chan32"))

            self.assertEqual(caught.exception.code, "too_many_channels")


if __name__ == "__main__":
    unittest.main()

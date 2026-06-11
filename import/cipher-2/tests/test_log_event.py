import json
import re
import unittest

from cipher2.tools.log import LogError, LogEvent


class LogEventTest(unittest.TestCase):
    def test_log_event_defaults_and_json_round_trip(self):
        event = LogEvent(event_name="storage.write", channel="storage")

        self.assertEqual(event.schema_version, 2)
        self.assertEqual(event.status, "ok")
        self.assertEqual(event.counts, {})
        self.assertEqual(event.payload, {})
        self.assertRegex(event.timestamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

        encoded = json.dumps(event.to_json(), sort_keys=True)
        decoded = LogEvent.from_json(json.loads(encoded))
        self.assertEqual(decoded, event)

    def test_log_event_accepts_legacy_v1_rows(self):
        event = LogEvent(event_name="storage.write", channel="storage", schema_version=1)

        decoded = LogEvent.from_json(event.to_json())

        self.assertEqual(decoded.schema_version, 1)
        self.assertEqual(decoded.event_name, "storage.write")

    def test_log_event_accepts_multi_segment_event_names_for_extractors(self):
        event = LogEvent(event_name="extractor.code.file", channel="initializer")

        self.assertEqual(event.event_name, "extractor.code.file")
        self.assertEqual(LogEvent.from_json(event.to_json()), event)

    def test_log_event_rejects_invalid_event_name_status_and_timestamp(self):
        cases = [
            {"event_name": "storage", "channel": "storage"},
            {"event_name": "Storage.write", "channel": "storage"},
            {"event_name": "storage.", "channel": "storage"},
            {"event_name": "storage.write", "channel": "storage", "status": "bad"},
            {
                "event_name": "storage.write",
                "channel": "storage",
                "timestamp": "2026-05-25T10:00:00Z",
            },
            {"event_name": "storage.write", "channel": "storage", "schema_version": 3},
            {"event_name": "storage.write", "channel": "storage", "schema_version": True},
        ]

        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(LogError):
                    LogEvent(**kwargs)

    def test_log_event_requires_error_code_for_error_status(self):
        with self.assertRaises(LogError) as caught:
            LogEvent(event_name="storage.error", channel="storage", status="error")

        self.assertEqual(caught.exception.code, "invalid_event")

    def test_log_event_rejects_non_json_payload_and_bad_counts(self):
        with self.assertRaises(LogError):
            LogEvent(event_name="storage.write", channel="storage", payload={"bad": object()})

        with self.assertRaises(LogError):
            LogEvent(event_name="storage.write", channel="storage", counts={"items": 1.5})


if __name__ == "__main__":
    unittest.main()

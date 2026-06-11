import tempfile
import unittest
from pathlib import Path

from cipher2.tools.log import LogError, LogEvent, open_log, safe_channel_name


class LogPathSafetyTest(unittest.TestCase):
    def test_safe_channel_name_accepts_expected_names(self):
        for channel in ["default", "log", "storage", "mcp", "views", "abc_123-xyz"]:
            with self.subTest(channel=channel):
                self.assertEqual(safe_channel_name(channel), channel)

    def test_safe_channel_name_rejects_escape_and_uppercase(self):
        for channel in ["", " ", "../x", "a/b", "Storage", "bad\x00name", "." * 2]:
            with self.subTest(channel=channel):
                with self.assertRaises(LogError):
                    safe_channel_name(channel)

    def test_write_stays_inside_target_cipher_log_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            result = open_log(target).write_event(
                LogEvent(event_name="storage.write", channel="storage")
            )

            self.assertTrue(result.path.is_relative_to(target / ".cipher" / "log"))
            self.assertFalse((Path.cwd() / ".cipher").exists())

    def test_invalid_channel_is_rejected_before_path_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            with self.assertRaises(LogError) as caught:
                log.write_event(LogEvent(event_name="storage.write", channel="../storage"))

            self.assertIn(caught.exception.code, {"invalid_channel", "path_escape"})


if __name__ == "__main__":
    unittest.main()

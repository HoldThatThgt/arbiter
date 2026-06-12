import tempfile
import unittest
from unittest import mock
from pathlib import Path

from arbiter_engine.gdbmcp.diagnostics import doctor, gdb_run_check


class DoctorTest(unittest.TestCase):
    def test_doctor_reports_missing_gdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = doctor(Path(tmp), gdb="/definitely/missing/gdb")
            self.assertFalse(payload["ok"])
            self.assertFalse([item for item in payload["checks"] if item["name"] == "gdb"][0]["ok"])

    def test_gdb_run_check_reports_missing_compiler(self):
        with mock.patch("shutil.which", return_value=None):
            payload = gdb_run_check("/fake/gdb")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["name"], "gdb_run")


if __name__ == "__main__":
    unittest.main()

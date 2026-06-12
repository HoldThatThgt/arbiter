import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from cipher2.cli import main


def _run(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(argv, stdout=stdout, stderr=stderr)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class CliPathSafetyTest(unittest.TestCase):
    def test_invalid_target_source_root_profile_and_compile_database_are_structured_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "repo"
            target.mkdir()
            outside = workspace / "outside.c"
            outside.write_text("int outside(void) { return 0; }\n", encoding="utf-8")

            cases = [
                (["init", str(workspace / "missing"), "--json"], "invalid_target"),
                (["init", str(target), "--source-root", str(outside), "--json"], "path_escape"),
                (["init", str(target), "--profile", "", "--json"], "invalid_profile"),
                (["init", str(target), "--compile-database", "missing.json", "--json"], "compile_database_unreadable"),
            ]
            for argv, expected_code in cases:
                with self.subTest(expected_code=expected_code):
                    exit_code, stdout, stderr = _run(argv)
                    payload = json.loads(stdout)

                    self.assertEqual(exit_code, 1)
                    self.assertEqual(payload["error"]["code"], expected_code)
                    self.assertIn(expected_code, stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertNotIn(str(target), stderr)

    def test_compile_database_inside_cipher_and_storage_lock_busy_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "compile_commands.json").write_text("[]\n", encoding="utf-8")

            exit_code, stdout, stderr = _run(
                ["init", str(target), "--compile-database", ".cipher/compile_commands.json", "--json"]
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(json.loads(stdout)["error"]["code"], "path_escape")
            self.assertIn("path_escape", stderr)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            lock_dir = target / ".cipher" / "run" / "storage.lock"
            lock_dir.mkdir(parents=True)
            (lock_dir / "owner.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

            exit_code, stdout, stderr = _run(["init", str(target), "--json"])

            self.assertEqual(exit_code, 1)
            self.assertEqual(json.loads(stdout)["error"]["code"], "storage_error")
            self.assertIn("storage_error", stderr)

    def test_log_write_failure_does_not_break_successful_init_or_leak_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "log").write_text("not a directory", encoding="utf-8")

            exit_code, stdout, stderr = _run(["init", str(target), "--json"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(stdout)["ok"])
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()

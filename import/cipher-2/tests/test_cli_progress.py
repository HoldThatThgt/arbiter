import io
import json
import tempfile
import unittest
from pathlib import Path

from cipher2.cli import main
from cipher2.config import write_default_config
from cipher2.tools.log import open_log
from tests.toolchain_helpers import write_fake_toolchain


class _TtyStringIO(io.StringIO):
    def isatty(self):
        return True


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(argv, *, stderr=None):
    stdout = io.StringIO()
    err = io.StringIO() if stderr is None else stderr
    exit_code = main(argv, stdout=stdout, stderr=err)
    return exit_code, stdout.getvalue(), err.getvalue()


class CliProgressTest(unittest.TestCase):
    def test_default_json_init_writes_progress_to_stderr_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "main.c", "int main(void) { return 0; }\n")
            write_fake_toolchain(target)

            exit_code, stdout, stderr = _run(["init", str(target), "--source-root", "src/main.c", "--json"])
            payload = json.loads(stdout)

        self.assertEqual(exit_code, 0, stderr)
        self.assertTrue(payload["ok"])
        self.assertIn("cipher2 init: sources=1", stderr)
        self.assertIn("1/1 src/main.c", stderr)
        self.assertIn("cipher2 init: done files=1/1", stderr)
        self.assertNotIn("src/main.c", stdout)

    def test_no_progress_suppresses_stderr_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "main.c", "int main(void) { return 0; }\n")
            write_fake_toolchain(target)

            exit_code, stdout, stderr = _run(
                ["init", str(target), "--source-root", "src/main.c", "--json", "--no-progress"]
            )

        self.assertEqual(exit_code, 0, stderr)
        self.assertTrue(json.loads(stdout)["ok"])
        self.assertEqual(stderr, "")

    def test_no_log_keeps_progress_but_writes_no_log_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "main.c", "int main(void) { return 0; }\n")
            write_fake_toolchain(target)

            exit_code, _stdout, stderr = _run(
                ["init", str(target), "--source-root", "src/main.c", "--json", "--no-log"]
            )
            summary = open_log(target).summarize()

        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("cipher2 init:", stderr)
        self.assertEqual(summary.total_events, 0)

    def test_tty_progress_uses_carriage_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "main.c", "int main(void) { return 0; }\n")
            write_fake_toolchain(target)
            stderr = _TtyStringIO()

            exit_code, _stdout, stderr_text = _run(
                ["init", str(target), "--source-root", "src/main.c", "--json"],
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0, stderr_text)
        self.assertIn("\r", stderr_text)
        self.assertTrue(stderr_text.endswith("\n"))

    def test_failure_progress_does_not_duplicate_cli_error_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "main.c", "int main(void) { return 0; }\n")
            clang = target / "bin" / "clang"
            clang.parent.mkdir(parents=True, exist_ok=True)
            clang.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            clang.chmod(0o755)
            write_default_config(target, clang_executable="bin/clang", observe=False)

            exit_code, stdout, stderr = _run(["init", str(target), "--source-root", "src/main.c", "--json"])
            payload = json.loads(stdout)

        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(stderr.count("cipher2: clang_capability_failed:"), 1)
        self.assertIn("cipher2 init: stopped elapsed=", stderr)
        self.assertNotIn("cipher2 init: stopped clang_capability_failed", stderr)


if __name__ == "__main__":
    unittest.main()

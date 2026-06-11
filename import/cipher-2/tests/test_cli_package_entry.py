import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return env


class CliPackageEntryTest(unittest.TestCase):
    def test_python_module_help_version_and_init_smoke(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "cipher2", "--help"],
            cwd=REPO_ROOT,
            env=_env(),
            text=True,
            capture_output=True,
            check=False,
        )
        version_result = subprocess.run(
            [sys.executable, "-m", "cipher2", "--version"],
            cwd=REPO_ROOT,
            env=_env(),
            text=True,
            capture_output=True,
            check=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            init_result = subprocess.run(
                [sys.executable, "-m", "cipher2", "init", tmp, "--json"],
                cwd=REPO_ROOT,
                env=_env(),
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("cipher2", help_result.stdout)
        self.assertEqual(version_result.returncode, 0, version_result.stderr)
        self.assertEqual(version_result.stdout.strip(), "cipher2 1.0.0")
        self.assertEqual(init_result.returncode, 0, init_result.stderr)
        self.assertTrue(json.loads(init_result.stdout)["ok"])

    def test_pyproject_declares_console_script_entry(self):
        pyproject = REPO_ROOT / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")

        self.assertIn('version = "1.0.0"', text)
        self.assertIn("[project.scripts]", text)
        self.assertIn('cipher2 = "cipher2.cli:main"', text)


if __name__ == "__main__":
    unittest.main()

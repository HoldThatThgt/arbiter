import os
import subprocess
import sys
import unittest
from pathlib import Path


class VersionCommandTest(unittest.TestCase):
    def test_module_prints_version(self):
        repo = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo / "engine")

        completed = subprocess.run(
            [sys.executable, "-m", "arbiter_engine", "--version"],
            cwd=repo,
            env=env,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "arbiter-engine dev\n")
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()

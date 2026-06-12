from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO

from arbiter_engine.perfmcp.cli import main


class MeasureCLITests(unittest.TestCase):
    def test_measure_cli_strips_double_dash_separator(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["measure", "--repeat", "1", "--", sys.executable, "-c", "print('ok')"])

        self.assertEqual(exit_code, 0)
        self.assertIn("successful_runs", output.getvalue())


if __name__ == "__main__":
    unittest.main()

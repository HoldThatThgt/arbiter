from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.perfmcp.analysis import measure_command, scan_c_project


class ScanAnalysisTests(unittest.TestCase):
    def test_scan_detects_c_perf_patterns_and_ignores_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src" / "hot.c"
            source.parent.mkdir()
            source.write_text(
                """
                #include <stdlib.h>
                #include <string.h>
                void hot(char *s, int n) {
                    // strlen(s) in a comment must not be reported.
                    for (int i = 0; i < strlen(s); i++) {
                        char *p = malloc(32);
                        p = realloc(p, i + 1);
                        for (int j = 0; j < n; j++) {
                            memset(p, 0, 32);
                        }
                        free(p);
                    }
                }
                """,
                encoding="utf-8",
            )

            payload = scan_c_project(
                root,
                paths=["src"],
                min_severity="low",
                include_low_confidence=True,
                max_findings=20,
            )

        rules = {finding["rule_id"] for finding in payload["findings"]}
        self.assertIn("C.PERF.STRLEN_IN_LOOP", rules)
        self.assertIn("C.PERF.ALLOC_IN_LOOP", rules)
        self.assertIn("C.PERF.REALLOC_GROW_ONE", rules)
        self.assertIn("C.PERF.NESTED_LOOP", rules)
        self.assertIn("C.PERF.BULK_MEMORY_IN_LOOP", rules)
        self.assertEqual(payload["warnings"], [])
        snippets = [finding["evidence"]["snippet"] for finding in payload["findings"]]
        self.assertFalse(any("comment must not" in snippet for snippet in snippets))
        ids = [finding["id"] for finding in payload["findings"]]
        self.assertEqual(ids, [f"PERF{index:04d}" for index in range(1, len(ids) + 1)])

    def test_scan_skips_paths_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.NamedTemporaryFile(suffix=".c") as outside:
            root = Path(tmp)
            payload = scan_c_project(root, paths=[outside.name])

        self.assertEqual(payload["files_scanned"], 0)
        self.assertTrue(any("outside root" in warning for warning in payload["warnings"]))


class MeasureTests(unittest.TestCase):
    def test_measure_command_uses_argv_and_reports_runs(self) -> None:
        payload = measure_command(
            [sys.executable, "-c", "print('ok')"],
            repeat=2,
            timeout_seconds=10,
            max_output_chars=2000,
        )

        self.assertEqual(payload["schema_version"], "perf-mcp.measure.v1")
        self.assertEqual(payload["summary"]["successful_runs"], 2)
        self.assertEqual(len(payload["runs"]), 2)
        self.assertTrue(all(run["stdout"].strip() == "ok" for run in payload["runs"]))

    def test_measure_rejects_shell_string(self) -> None:
        payload = measure_command("echo unsafe")  # type: ignore[arg-type]
        self.assertTrue(payload["is_error"])


if __name__ == "__main__":
    unittest.main()

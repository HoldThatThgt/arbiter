import io
import json
import tempfile
import unittest
from pathlib import Path

from cipher2.cli import CliResult, CliWarning, main
from cipher2.initializer import InitStageTiming


def _run(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(argv, stdout=stdout, stderr=stderr)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class CliOutputTest(unittest.TestCase):
    def test_human_success_output_includes_summary_and_stage_timings(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, stdout, stderr = _run(["init", str(target), "--no-progress"])

        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertRegex(stdout, r"^initialized snapshot=sha256-[0-9a-f]+ facts=0 relatives=0 sources=0 warnings=0\n")
        self.assertIn("stages: collect=", stdout)
        self.assertIn("read_index=", stdout)
        self.assertIn("setup: compile_db=not_found clang=not_run mcp=.mcp.json:created setup_warnings=1\n", stdout)
        self.assertIn("warning: compile_database_not_found:", stdout)

    def test_rebuild_human_success_output_uses_rebuilt_verb(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, stdout, stderr = _run(["rebuild", str(target)])

        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertRegex(stdout, r"^rebuilt snapshot=sha256-[0-9a-f]+ facts=0 relatives=0 sources=0 warnings=0\n")
        self.assertIn("stages: collect=", stdout)

    def test_json_success_output_is_stable_object_and_errors_use_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, stdout, stderr = _run(["init", str(target), "--json"])
            payload = json.loads(stdout)

        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("cipher2 init: done files=0/0", stderr)
        self.assertEqual(
            sorted(payload),
            [
                "command",
                "duration_ms",
                "fact_count",
                "ok",
                "relative_count",
                "setup",
                "snapshot_id",
                "source_count",
                "stage_timings",
                "warning_count",
            ],
        )
        self.assertEqual([stage["stage"] for stage in payload["stage_timings"]], [
            "collect",
            "extract",
            "reduce",
            "resolve",
            "relative_merge",
            "snapshot_write",
            "read_index",
        ])
        self.assertEqual(payload["setup"]["compile_database"]["action"], "not_found")
        self.assertEqual(payload["setup"]["mcp_config"]["action"], "created")
        self.assertEqual(payload["setup"]["toolchain"]["status"], "not_run")

    def test_json_success_output_includes_bounded_warning_list_when_present(self):
        result = CliResult(
            ok=True,
            exit_code=0,
            command="init",
            snapshot_id="sha256-test",
            fact_count=1,
            relative_count=0,
            source_count=1,
            warning_count=1,
            duration_ms=1.0,
            stage_timings=(InitStageTiming("collect", 1.0),),
            warnings=(
                CliWarning(
                    code="clang_ast_partial",
                    message="clang AST invocation produced partial output",
                    source="src/large.c",
                    details={"diagnostic_kind": "partial_ast", "reason": "nonzero_exit"},
                ),
            ),
        )

        payload = result.to_json()

        self.assertEqual(payload["warning_count"], 1)
        self.assertEqual(payload["stage_timings"][0]["stage"], "collect")
        self.assertEqual(payload["warnings"][0]["code"], "clang_ast_partial")
        self.assertEqual(payload["warnings"][0]["source"], "src/large.c")
        self.assertEqual(payload["warnings"][0]["details"]["reason"], "nonzero_exit")

    def test_json_failure_still_writes_stderr_and_structured_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"

            exit_code, stdout, stderr = _run(["init", str(missing), "--json"])
            payload = json.loads(stdout)

        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "init")
        self.assertEqual(payload["error"]["code"], "invalid_target")
        self.assertIn("cipher2: invalid_target:", stderr)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()

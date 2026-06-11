import io
import tempfile
import unittest
from pathlib import Path

from cipher2.cli import main
from cipher2.tools.log import open_log
from cipher2.tools.views import build_overview
from tests.toolchain_helpers import write_fake_toolchain


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(argv, stdout=stdout, stderr=stderr)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class CliObservabilityTest(unittest.TestCase):
    def test_success_cli_command_event_is_visible_in_log_view_with_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "main.c", "int main(void) { return 0; }\n")
            write_fake_toolchain(target)

            exit_code, _stdout, stderr = _run(["init", str(target), "--source-root", "src/main.c", "--json"])
            overview = build_overview(target, include_sections=["log"], top_n=10)

        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(overview.log.events_by_channel["cli"], 3)
        self.assertIn(("cli.command", 1), overview.log.top_event_names)
        self.assertIn(("cli.setup_discovery", 1), overview.log.top_event_names)
        self.assertIn(("cli.mcp_config", 1), overview.log.top_event_names)
        fields = [field for row in overview.log.recent_events for field in row.fields if row.label == "cli.command"]
        self.assertEqual(
            [name for name, _value in fields[:12]],
            [
                "duration_ms",
                "count.fact_count",
                "count.relative_count",
                "count.source_count",
                "count.warning_count",
                "operation",
                "outcome",
                "command_name",
                "exit_code",
                "json_output",
                "source_root_count",
                "profile",
            ],
        )
        self.assertIn(("command_name", "init"), fields)
        self.assertIn(("exit_code", "0"), fields)
        self.assertIn(("json_output", "True"), fields)
        self.assertIn(("source_root_count", "1"), fields)
        self.assertTrue(any(name == "count.fact_count" for name, _value in fields))
        self.assertNotIn(str(target), str(overview.log.recent_events))

    def test_init_wires_build_readiness_preflight_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, _stdout, stderr = _run(["init", str(target), "--no-progress", "--json"])
            events = open_log(target).read_events(channel="initializer").events

        self.assertEqual(exit_code, 0, stderr)
        readiness = next(event for event in events if event.event_name == "initializer.build_readiness")
        self.assertEqual(readiness.status, "ok")
        self.assertEqual(readiness.payload["operation"], "build_readiness")
        self.assertEqual(readiness.payload["outcome"], "ready")
        self.assertEqual(readiness.payload["has_compile_database"], False)
        self.assertEqual(readiness.payload["clang_ready"], True)
        self.assertEqual(readiness.payload["gcc_ready"], True)
        self.assertEqual(readiness.counts["missing_input_count"], 0)
        self.assertNotIn(str(target), str(readiness.to_json()))

    def test_failure_cli_error_event_is_visible_without_traceback_or_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            outside = Path(tmp).parent / "outside.c"
            outside.write_text("int outside(void) { return 0; }\n", encoding="utf-8")

            exit_code, _stdout, stderr = _run(["init", str(target), "--source-root", str(outside), "--json"])
            overview = build_overview(target, include_sections=["log"], top_n=10)

        self.assertEqual(exit_code, 1)
        self.assertIn("path_escape", stderr)
        self.assertEqual(overview.log.events_by_status["error"], 2)
        self.assertIn("path_escape", overview.log.error_codes)
        cli_error = next(row for row in overview.log.recent_events if row.label == "cli.error")
        self.assertEqual(cli_error.error_code, "path_escape")
        self.assertNotIn("Traceback", str(cli_error))
        self.assertNotIn(str(target), str(cli_error))

    def test_no_log_suppresses_cli_and_downstream_channel_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, _stdout, stderr = _run(["init", str(target), "--no-log", "--json"])
            summary = open_log(target).summarize()

        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(summary.total_events, 0)

    def test_status_cli_event_is_visible_in_log_view_with_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, stdout, stderr = _run(["status", str(target), "--json"])
            overview = build_overview(target, include_sections=["log"], top_n=10)

        self.assertEqual(exit_code, 0, stderr)
        self.assertIn('"state":"empty"', stdout)
        self.assertGreaterEqual(overview.log.events_by_channel["cli"], 1)
        self.assertIn(("cli.status", 1), overview.log.top_event_names)
        status_row = next(row for row in overview.log.recent_events if row.label == "cli.status")
        self.assertEqual(status_row.status, "ok")
        self.assertIn(("operation", "status"), status_row.fields)
        self.assertIn(("outcome", "rendered"), status_row.fields)
        self.assertIn(("command_name", "status"), status_row.fields)
        self.assertIn(("json_output", "True"), status_row.fields)
        self.assertIn(("overview_state", "empty"), status_row.fields)
        self.assertIn("section_count=3", status_row.detail)
        self.assertIn("error_count=0", status_row.detail)
        self.assertNotIn(str(target), str(status_row))


if __name__ == "__main__":
    unittest.main()

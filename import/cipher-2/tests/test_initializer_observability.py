import tempfile
import unittest
from pathlib import Path

from cipher2.initializer import InitError, initialize_repository
from cipher2.tools.log import open_log
from cipher2.tools.views import build_overview
from tests.toolchain_helpers import write_fake_toolchain


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class InitializerObservabilityTest(unittest.TestCase):
    def test_init_stage_events_are_stable_sanitized_and_use_reduce_accumulator(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int helper(void) { return 1; }\nint entry(void) { return helper(); }\n")
            write_fake_toolchain(target)

            summary = initialize_repository(target, profile="debug")

            self.assertEqual([stage.stage for stage in summary.stage_timings], [
                "collect",
                "extract",
                "reduce",
                "resolve",
                "relative_merge",
                "snapshot_write",
                "read_index",
            ])
            events = [
                event for event in open_log(target).read_events(channel="initializer").events
                if event.event_name == "init.stage"
            ]
            self.assertEqual([event.payload["stage"] for event in events], [
                "collect",
                "extract",
                "reduce",
                "resolve",
                "relative_merge",
                "snapshot_write",
                "read_index",
            ])
            reduce_stage = next(event for event in events if event.payload["stage"] == "reduce")
            self.assertEqual(reduce_stage.payload["mode"], "per_file_outcome_accumulator")
            self.assertEqual(reduce_stage.counts["reduce_outcome_count"], 1)
            self.assertEqual(reduce_stage.counts["fact_count"], summary.fact_count)
            self.assertGreaterEqual(reduce_stage.duration_ms, 0.0)
            snapshot_stage = next(event for event in events if event.payload["stage"] == "snapshot_write")
            self.assertIn("compressed_data_bytes", snapshot_stage.counts)
            self.assertNotIn("bytes_written", snapshot_stage.counts)
            for event in events:
                self.assertEqual(event.status, "ok")
                self.assertEqual(event.payload["outcome"], "stage_completed")
                self.assertNotIn(str(target), str(event.to_json()))
                self.assertNotIn("return helper", str(event.to_json()))

    def test_success_events_are_visible_in_log_view_with_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int helper(void) { return 1; }\nint entry(void) { return helper(); }\n")
            write_fake_toolchain(target)

            summary = initialize_repository(target, profile="debug")

            self.assertTrue(summary.ok)
            events = open_log(target).read_events(channel="initializer").events
            names = [event.event_name for event in events]
            self.assertIn("extractor.code.file", names)
            self.assertIn("initializer.run", names)
            toolchain = next(event for event in events if event.event_name == "extractor.code.toolchain")
            file_event = next(event for event in events if event.event_name == "extractor.code.file")
            self.assertEqual(toolchain.payload["type_driven_ast"], True)
            self.assertEqual(toolchain.payload["loc_file_supported"], True)
            self.assertEqual(toolchain.payload["call_reference_supported"], True)
            self.assertEqual(toolchain.payload["member_reference_supported"], True)
            self.assertEqual(toolchain.payload["qual_type_supported"], True)
            self.assertIn("typed_call_expr_count", file_event.counts)
            self.assertIn("source_fallback_count", file_event.counts)
            self.assertIn("record_owner_count", file_event.counts)
            self.assertIn("anonymous_record_count", file_event.counts)
            self.assertIn("synthetic_type_fact_count", file_event.counts)
            self.assertIn("field_decl_count", file_event.counts)
            self.assertIn("field_fact_count", file_event.counts)
            self.assertIn("field_decl_without_fact_count", file_event.counts)
            self.assertIn("field_access_resolved_count", file_event.counts)
            self.assertIn("field_access_unresolved_count", file_event.counts)
            run = next(event for event in events if event.event_name == "initializer.run")
            self.assertEqual(run.status, "ok")
            self.assertEqual(run.payload["operation"], "initialize_repository")
            self.assertEqual(run.payload["outcome"], "written")
            self.assertEqual(run.payload["profile"], "debug")
            self.assertEqual(run.counts["fact_count"], summary.fact_count)
            self.assertEqual(run.counts["source_count"], 1)
            self.assertNotIn(str(target), str([event.to_json() for event in events]))
            self.assertNotIn("return helper", str([event.to_json() for event in events]))

            overview = build_overview(target, include_sections=["log"], top_n=10)
            self.assertIsNotNone(overview.log)
            self.assertEqual(overview.log.events_by_channel["initializer"], len(events))
            self.assertIn(("initializer.run", 1), overview.log.top_event_names)
            row = next(row for row in overview.log.recent_events if row.label == "initializer.run")
            self.assertIn(("count.fact_count", str(summary.fact_count)), row.fields)
            self.assertIn(("count.source_count", "1"), row.fields)
            self.assertIn(("operation", "initialize_repository"), row.fields)

    def test_failure_events_have_stable_error_codes_without_traceback_or_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            with self.assertRaises(InitError):
                initialize_repository(target, source_roots=["missing.c"])

            event = next(item for item in open_log(target).read_events(channel="initializer").events if item.event_name == "initializer.error")
            self.assertEqual(event.status, "error")
            self.assertEqual(event.error_code, "invalid_source_root")
            self.assertEqual(event.payload["outcome"], "failed")
            self.assertNotIn("Traceback", str(event.to_json()))
            self.assertNotIn(str(target), str(event.to_json()))
            overview = build_overview(target, include_sections=["log"], top_n=10)
            self.assertEqual(overview.log.error_codes["invalid_source_root"], 1)

    def test_log_write_failure_does_not_break_initializer_main_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int entry(void) { return 0; }\n")
            write_fake_toolchain(target)
            (target / ".cipher").mkdir(exist_ok=True)
            (target / ".cipher" / "log").write_text("not a directory", encoding="utf-8")

            summary = initialize_repository(target)

            self.assertTrue(summary.ok)
            self.assertEqual(summary.warning_count, 0)
            self.assertGreater(summary.fact_count, 0)


if __name__ == "__main__":
    unittest.main()

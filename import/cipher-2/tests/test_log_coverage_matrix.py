import importlib
import unittest


EXPECTED_TESTS = {
    "event schema defaults and json round trip": [
        "tests.test_log_event.LogEventTest.test_log_event_defaults_and_json_round_trip",
        "tests.test_log_event.LogEventTest.test_log_event_accepts_multi_segment_event_names_for_extractors",
    ],
    "event validation errors": [
        "tests.test_log_event.LogEventTest.test_log_event_rejects_invalid_event_name_status_and_timestamp",
        "tests.test_log_event.LogEventTest.test_log_event_requires_error_code_for_error_status",
        "tests.test_log_event.LogEventTest.test_log_event_rejects_non_json_payload_and_bad_counts",
    ],
    "append jsonl redaction truncation and observe batch": [
        "tests.test_log_writer.LogWriterTest.test_append_jsonl_redacts_and_truncates_payload",
        "tests.test_log_writer.LogWriterTest.test_observe_batch_writes_to_original_channel_without_recursion",
        "tests.test_log_writer.LogWriterTest.test_read_and_summary_emit_log_channel_observability_events",
    ],
    "write failure and channel limits": [
        "tests.test_log_writer.LogWriterTest.test_write_failure_increments_dropped_count_and_reports_once",
        "tests.test_log_writer.LogWriterTest.test_too_many_channels_is_rejected",
    ],
    "reader filters recovery and limit": [
        "tests.test_log_reader.LogReaderTest.test_read_events_filters_channel_limit_and_time_window",
        "tests.test_log_reader.LogReaderTest.test_malformed_and_oversized_lines_are_reported_without_failing_read",
        "tests.test_log_reader.LogReaderTest.test_limit_zero_returns_empty_result",
        "tests.test_log_reader.LogReaderTest.test_invalid_schema_rows_are_reported_and_skipped",
    ],
    "summary aggregation and bounded digests": [
        "tests.test_log_summary.LogSummaryTest.test_summary_aggregates_counts_status_errors_and_bytes",
        "tests.test_log_summary.LogSummaryTest.test_recent_and_slow_events_are_bounded_and_sorted_by_contract",
        "tests.test_log_summary.LogSummaryTest.test_digest_counts_fields_limits_and_order_are_stable",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_init_stage_fields_and_latest_stage_summary",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_toolchain_capability_fields",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_file_warning_diagnostic_kind",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_field_coverage_fields",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_function_pointer_dispatch_fields",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_mcp_relative_preview_quality_fields",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_compile_database_fields",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_direct_call_resolution_fields",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_worker_pool_fields",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_worker_relative_dedup_fields",
        "tests.test_log_summary.LogSummaryTest.test_digest_includes_relative_merge_fields",
        "tests.test_log_summary.LogSummaryTest.test_summary_records_redaction_truncation_and_malformed_lines",
    ],
    "path safety": [
        "tests.test_log_path_safety.LogPathSafetyTest.test_safe_channel_name_accepts_expected_names",
        "tests.test_log_path_safety.LogPathSafetyTest.test_safe_channel_name_rejects_escape_and_uppercase",
        "tests.test_log_path_safety.LogPathSafetyTest.test_write_stays_inside_target_cipher_log_directory",
        "tests.test_log_path_safety.LogPathSafetyTest.test_invalid_channel_is_rejected_before_path_construction",
    ],
    "views input contract": [
        "tests.test_log_summary_for_views.LogSummaryForViewsTest.test_summary_exposes_view_model_inputs_without_read_events",
        "tests.test_log_summary_for_views.LogSummaryForViewsTest.test_empty_log_summary_supports_empty_view_state",
    ],
    "performance and concurrency": [
        "tests.test_log_performance.LogPerformanceTest.test_small_medium_large_workloads_stay_within_memory_budgets",
        "tests.test_log_performance.LogPerformanceTest.test_concurrent_appends_preserve_complete_jsonl_rows",
    ],
}


class LogCoverageMatrixTest(unittest.TestCase):
    def test_log_design_matrix_has_behavior_tests_for_every_requirement(self):
        missing = []
        for requirement, test_names in EXPECTED_TESTS.items():
            self.assertGreater(len(test_names), 0, requirement)
            for test_name in test_names:
                if not self._test_exists(test_name):
                    missing.append(f"{requirement}: {test_name}")

        self.assertEqual(missing, [])

    def _test_exists(self, test_name: str) -> bool:
        module_name, class_name, method_name = test_name.rsplit(".", 2)
        module = importlib.import_module(module_name)
        test_class = getattr(module, class_name, None)
        return test_class is not None and callable(getattr(test_class, method_name, None))


if __name__ == "__main__":
    unittest.main()

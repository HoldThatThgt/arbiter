import importlib
import unittest


EXPECTED_TESTS = {
    "storage view states": [
        "tests.test_views_storage_model.ViewsStorageModelTest.test_empty_storage_model_is_empty_without_creating_snapshot",
        "tests.test_views_storage_model.ViewsStorageModelTest.test_ready_storage_model_combines_stats_and_storage_log_summary",
        "tests.test_views_storage_model.ViewsStorageModelTest.test_log_degraded_storage_model_is_warning",
        "tests.test_views_storage_model.ViewsStorageModelTest.test_stale_lock_storage_model_is_warning",
        "tests.test_views_storage_model.ViewsStorageModelTest.test_storage_stats_failure_is_section_error_without_traceback",
        "tests.test_views_relatives.ViewsRelativesTest.test_storage_model_exposes_relative_stats_and_relations_count",
    ],
    "log view states and rows": [
        "tests.test_views_log_model.ViewsLogModelTest.test_empty_log_model_is_empty",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_renders_recent_slow_rows_and_top_events",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_exposes_latest_init_stage_timings_outside_recent_window",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_warning_state_for_malformed_lines_and_warning_events",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_exposes_toolchain_and_file_warning_state",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_exposes_missing_type_driven_evidence",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_warns_on_field_coverage_gaps",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_warns_on_dispatch_gaps",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_exposes_mcp_relative_preview_quality_counts",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_warns_on_compile_database_miss",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_exposes_direct_call_resolution_warning_counts",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_model_exposes_worker_pool_counts",
        "tests.test_views_log_model.ViewsLogModelTest.test_log_error_section_is_isolated",
        "tests.test_cli_observability.CliObservabilityTest.test_status_cli_event_is_visible_in_log_view_with_counts",
    ],
    "overview and synthetic errors": [
        "tests.test_views_overview.ViewsOverviewTest.test_include_sections_empty_returns_empty_without_sections",
        "tests.test_views_overview.ViewsOverviewTest.test_default_overview_merges_storage_and_log_ready_state",
        "tests.test_views_overview.ViewsOverviewTest.test_section_failure_does_not_drop_other_sections",
        "tests.test_views_overview.ViewsOverviewTest.test_invalid_request_returns_synthetic_errors",
        "tests.test_incremental_observability.IncrementalObservabilityTest.test_incremental_log_events_are_visible_in_views_incremental_section",
    ],
    "views observability": [
        "tests.test_views_observability.ViewsObservabilityTest.test_successful_build_writes_views_build_event",
        "tests.test_views_observability.ViewsObservabilityTest.test_section_error_writes_views_section_error_event",
        "tests.test_views_observability.ViewsObservabilityTest.test_storage_log_summary_failure_adds_storage_log_error",
    ],
    "views performance": [
        "tests.test_views_performance.ViewsPerformanceTest.test_small_medium_large_view_builds_stay_within_memory_budgets",
    ],
}


class ViewsCoverageMatrixTest(unittest.TestCase):
    def test_views_design_matrix_has_behavior_tests_for_every_requirement(self):
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

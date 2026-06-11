import importlib
import unittest


EXPECTED_TESTS = {
    "parser and usage": [
        "tests.test_cli_parser.CliParserTest.test_parse_init_defaults_and_repeated_source_roots",
        "tests.test_cli_parser.CliParserTest.test_parse_no_log_and_compile_database_are_single_invocation_options",
        "tests.test_cli_parser.CliParserTest.test_interactive_option_is_removed",
        "tests.test_cli_parser.CliParserTest.test_parse_rebuild_uses_same_options_with_rebuild_command",
        "tests.test_cli_parser.CliParserTest.test_parse_status_only_accepts_target_and_json",
        "tests.test_cli_parser.CliParserTest.test_usage_errors_exit_two_without_traceback",
        "tests.test_cli_parser.CliParserTest.test_status_usage_errors_exit_two_without_traceback",
    ],
    "init command behavior": [
        "tests.test_cli_init_command.CliInitCommandTest.test_init_empty_repository_writes_config_snapshot_and_json_summary",
        "tests.test_cli_init_command.CliInitCommandTest.test_init_with_source_roots_profile_and_compile_database_writes_expected_facts",
        "tests.test_cli_init_command.CliInitCommandTest.test_existing_config_is_preserved_without_compile_database_override",
        "tests.test_cli_rebuild_command.CliRebuildCommandTest.test_rebuild_runs_full_snapshot_write_and_returns_rebuild_command",
    ],
    "output": [
        "tests.test_cli_output.CliOutputTest.test_human_success_output_includes_summary_and_stage_timings",
        "tests.test_cli_output.CliOutputTest.test_rebuild_human_success_output_uses_rebuilt_verb",
        "tests.test_cli_output.CliOutputTest.test_json_success_output_is_stable_object_and_errors_use_stderr",
        "tests.test_cli_output.CliOutputTest.test_json_failure_still_writes_stderr_and_structured_stdout",
        "tests.test_cli_status_command.CliStatusCommandTest.test_status_human_output_renders_sections_without_creating_snapshot",
        "tests.test_cli_status_command.CliStatusCommandTest.test_status_json_output_is_full_tools_overview_model",
        "tests.test_cli_status_command.CliStatusCommandTest.test_status_section_error_is_rendered_without_failing_or_traceback",
    ],
    "observability and views": [
        "tests.test_cli_observability.CliObservabilityTest.test_success_cli_command_event_is_visible_in_log_view_with_counts",
        "tests.test_cli_observability.CliObservabilityTest.test_init_wires_build_readiness_preflight_event",
        "tests.test_cli_observability.CliObservabilityTest.test_status_cli_event_is_visible_in_log_view_with_counts",
        "tests.test_cli_rebuild_command.CliRebuildCommandTest.test_rebuild_writes_initializer_and_incremental_observability_events",
        "tests.test_cli_observability.CliObservabilityTest.test_failure_cli_error_event_is_visible_without_traceback_or_absolute_paths",
        "tests.test_cli_observability.CliObservabilityTest.test_no_log_suppresses_cli_and_downstream_channel_side_effects",
    ],
    "path safety and exception branches": [
        "tests.test_cli_path_safety.CliPathSafetyTest.test_invalid_target_source_root_profile_and_compile_database_are_structured_errors",
        "tests.test_cli_path_safety.CliPathSafetyTest.test_compile_database_inside_cipher_and_storage_lock_busy_are_reported",
        "tests.test_cli_path_safety.CliPathSafetyTest.test_log_write_failure_does_not_break_successful_init_or_leak_traceback",
        "tests.test_cli_status_command.CliStatusCommandTest.test_status_invalid_target_writes_stderr_without_status_stdout",
        "tests.test_cli_status_command.CliStatusCommandTest.test_status_log_write_failure_does_not_break_output",
    ],
    "package entry": [
        "tests.test_cli_package_entry.CliPackageEntryTest.test_python_module_help_version_and_init_smoke",
        "tests.test_cli_package_entry.CliPackageEntryTest.test_pyproject_declares_console_script_entry",
    ],
    "performance and smallness": [
        "tests.test_cli_performance.CliPerformanceTest.test_small_medium_large_cli_unit_workloads_stay_within_memory_budgets",
    ],
}


class CliCoverageMatrixTest(unittest.TestCase):
    def test_cli_design_matrix_has_behavior_tests_for_every_requirement(self):
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

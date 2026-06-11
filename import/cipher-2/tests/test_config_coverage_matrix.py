import importlib
import unittest


EXPECTED_TESTS = {
    "defaults and derived paths": [
        "tests.test_config_defaults.ConfigDefaultsTest.test_missing_config_returns_defaults_and_derives_paths",
        "tests.test_config_defaults.ConfigDefaultsTest.test_observe_false_does_not_create_cipher_directory",
        "tests.test_config_defaults.ConfigDefaultsTest.test_to_mapping_serializes_only_persistent_schema",
        "tests.test_config_defaults.ConfigDefaultsTest.test_auto_extractor_worker_count_caps_at_32_cpus",
        "tests.test_config_defaults.ConfigDefaultsTest.test_safe_cipher_path_returns_generated_path_inside_cipher_dir",
        "tests.test_config_defaults.ConfigDefaultsTest.test_missing_config_observed_as_default_load_event",
    ],
    "file schema and overrides": [
        "tests.test_config_file.ConfigFileTest.test_write_default_config_uses_config_yml_schema_and_atomic_tmp_cleanup",
        "tests.test_config_file.ConfigFileTest.test_load_existing_relative_compile_database_is_repo_relocatable",
        "tests.test_config_file.ConfigFileTest.test_absolute_external_compile_database_is_allowed_and_revalidated",
        "tests.test_config_file.ConfigFileTest.test_overrides_take_precedence_without_writing_config_file",
        "tests.test_config_file.ConfigFileTest.test_load_existing_clang_executable_and_args",
        "tests.test_config_file.ConfigFileTest.test_libclang_library_path_is_last_resort_readable_path",
        "tests.test_config_file.ConfigFileTest.test_invalid_libclang_library_path_is_rejected",
        "tests.test_config_file.ConfigFileTest.test_extractor_worker_count_loads_explicit_auto_and_overrides",
        "tests.test_config_file.ConfigFileTest.test_invalid_extractor_worker_count_is_rejected",
        "tests.test_config_file.ConfigFileTest.test_invalid_schema_version_raises_config_error",
        "tests.test_config_file.ConfigFileTest.test_invalid_scalar_and_malformed_yaml_raise_invalid_config",
        "tests.test_config_incremental.ConfigIncrementalTest.test_default_config_exposes_incremental_defaults_and_mapping",
        "tests.test_config_incremental.ConfigIncrementalTest.test_write_and_load_incremental_config_values",
        "tests.test_config_incremental.ConfigIncrementalTest.test_invalid_incremental_ranges_are_rejected",
        "tests.test_config_graph_inference.ConfigGraphInferenceTest.test_defaults_do_not_expose_graph_or_inference_config",
        "tests.test_config_graph_inference.ConfigGraphInferenceTest.test_legacy_graph_and_inference_sections_are_ignored_with_warning",
    ],
    "path safety and exception branches": [
        "tests.test_config_path_safety.ConfigPathSafetyTest.test_safe_cipher_path_rejects_parent_escape",
        "tests.test_config_path_safety.ConfigPathSafetyTest.test_safe_cipher_path_rejects_symlink_escape",
        "tests.test_config_path_safety.ConfigPathSafetyTest.test_safe_cipher_path_rejects_cipher_directory_symlink_escape",
        "tests.test_config_path_safety.ConfigPathSafetyTest.test_load_config_rejects_cipher_directory_symlink_escape_before_reading",
        "tests.test_config_path_safety.ConfigPathSafetyTest.test_compile_database_inside_cipher_directory_is_rejected",
        "tests.test_config_path_safety.ConfigPathSafetyTest.test_compile_database_unreadable_or_empty_path_is_rejected",
        "tests.test_config_path_safety.ConfigPathSafetyTest.test_non_string_compile_database_path_is_invalid_config",
        "tests.test_config_path_safety.ConfigPathSafetyTest.test_posix_backslash_relative_path_is_normalized",
    ],
    "observability and views": [
        "tests.test_config_observability.ConfigObservabilityTest.test_write_default_config_writes_config_write_without_absolute_paths",
        "tests.test_config_observability.ConfigObservabilityTest.test_load_config_writes_loaded_event_and_views_log_exposes_config_stats",
        "tests.test_config_observability.ConfigObservabilityTest.test_invalid_config_writes_config_error_with_stable_code_without_traceback",
        "tests.test_config_observability.ConfigObservabilityTest.test_log_write_failure_does_not_break_config_main_flow",
    ],
    "performance": [
        "tests.test_config_performance.ConfigPerformanceTest.test_small_medium_large_config_loads_stay_within_memory_budgets",
    ],
}


class ConfigCoverageMatrixTest(unittest.TestCase):
    def test_config_design_matrix_has_behavior_tests_for_every_requirement(self):
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

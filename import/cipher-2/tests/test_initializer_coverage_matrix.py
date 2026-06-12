import importlib
import unittest


EXPECTED_TESTS = {
    "initializer api and storage write": [
        "tests.test_initializer_api.InitializerApiTest.test_init_error_round_trips_for_process_worker_errors",
        "tests.test_initializer_api.InitializerApiTest.test_empty_repository_writes_empty_fact_snapshot_and_summary",
        "tests.test_initializer_api.InitializerApiTest.test_single_file_initialization_writes_storage_facts_and_is_idempotent",
        "tests.test_initializer_api.InitializerApiTest.test_initialization_streams_without_collect_materialization",
        "tests.test_initializer_api.InitializerApiTest.test_explicit_source_roots_and_profile_limit_scanned_files",
    ],
    "code extractor fact contract and fixtures": [
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_direct_call_evidence_json_round_trips_all_fields",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_code_fact_to_fact_record_preserves_contract_fields",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_code_fact_is_frozen_slotted",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_streaming_reducer_spools_snapshot_shaped_fact_lines",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_direct_call_resolver_rebuilds_index_from_spooled_scalars",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_external_relative_merge_uses_sorted_sidecar_without_full_payload_parse",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_external_relative_merge_within_fan_in_uses_single_pass",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_external_relative_merge_over_fan_in_uses_multipass_bounded_segments",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_external_relative_merge_fails_closed_on_non_exact_duplicate_id",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_worker_relative_dedup_skips_exact_duplicate_before_segment_write",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_worker_relative_dedup_fails_closed_on_non_exact_duplicate_id",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_worker_relative_dedup_saturation_keeps_untracked_relatives",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_map_reduce_duplicate_fact_prefers_min_source_seq_then_strict_superset",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_extractor_emits_expected_fact_kinds_from_c_fixture",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_header_and_multiple_source_files_are_scanned_deterministically",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_clang_member_expr_emits_field_read_and_write_relatives",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_unstable_field_access_operator_context_is_partial_read",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_wrapped_macro_bitwise_member_exprs_keep_field_access_semantics",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_function_pointer_dispatch_uses_field_global_and_local_slots",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_function_pointer_dispatch_uses_desugared_member_typedef_type",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_function_pointer_dispatch_does_not_guess_non_pointer_member_call",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_clang_implicit_line_numbers_inherit_previous_explicit_line_for_relatives",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_clang_same_line_duplicate_relative_ids_are_deduped_in_mapper",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_anonymous_union_fields_materialize_and_resolve_member_access",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_indirect_field_without_canonical_target_reports_field_gap",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_same_line_anonymous_records_have_distinct_synthetic_owners",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_missing_type_fact_still_materializes_named_record_fields",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_header_inline_uses_loc_file_identity_across_translation_units",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_header_inline_uses_inherited_header_context_when_function_loc_file_is_omitted",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_header_field_identity_merges_declaration_across_translation_units",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_header_field_identity_ignores_included_from_when_loc_file_is_omitted",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_header_field_identity_prefers_macro_expansion_location_over_spelling_define_site",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_same_named_header_fields_remain_distinct_for_different_structs",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_libclang_cursor_header_cache_skips_published_header_decl_subtree",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_header_cache_visibility_is_fixed_per_worker_context",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_header_resolver_seed_preserves_field_relations_for_cached_record",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_static_same_name_functions_in_different_sources_have_distinct_ids",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_unresolved_referenced_call_is_bounded_evidence_not_relation",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_cross_file_direct_call_resolves_unique_header_declaration_with_condition",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_clang_ast_guard_nodes_populate_conditions_without_fixture_keys",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_real_clang_ast_populates_guard_conditions_for_calls_and_fields",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_cross_file_direct_call_exact_source_wins_over_ambiguous_name",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_cross_file_direct_call_ambiguous_same_name_is_not_guessed",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_cross_file_direct_call_does_not_cross_internal_linkage",
        "tests.test_code_extractor_fixtures.CodeExtractorFixturesTest.test_direct_call_resolution_dedupes_and_counts_missing_callers",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_llvm_clang_17_capability_probe_is_accepted",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_apple_clang_21_capability_probe_is_accepted_without_gcc",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_gcc_version_is_not_checked_on_ast_only_path",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_clang_capability_probe_rejects_non_json_output",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_clang_capability_probe_rejects_missing_probe_function",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_clang_capability_probe_rejects_missing_type_driven_evidence",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_target_ast_failure_stays_distinct_from_capability_failure",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_partial_ast_returncode_error_accepts_valid_ast_with_warning_and_inventory",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_partial_ast_stderr_error_with_zero_returncode_is_warning",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_empty_translation_unit_inner_remains_file_ast_failure",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_partial_ast_recovery_subtree_does_not_emit_error_facts_or_relatives",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_file_ast_malformed_warning_records_diagnostic_kind",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_file_ast_timeout_warning_records_diagnostic_kind",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_file_ast_failure_is_warning_and_other_files_continue",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_all_file_ast_failures_still_write_snapshot_with_warnings",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_toolchain_and_file_warning_events_are_observable_and_sanitized",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_runtime_fails_closed_when_libclang_cannot_be_located",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_resolver_uses_configured_library_only_after_auto_fails",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_backend_tries_configured_library_after_auto_version_mismatch",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_version_mismatch_fails_without_configured_escape_hatch",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_missing_required_symbol_is_unavailable",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_missing_optional_opcode_symbols_still_configures",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_diagnostic_reason_uses_severity",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_cursor_kinds_normalize_to_json_mapper_vocabulary",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_cursor_to_ast_normalizes_native_libclang_kinds_before_probe_and_mapping",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_cursor_to_ast_prunes_external_header_subtrees_but_keeps_repo_headers",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_probe_keeps_tempfile_ast_outside_target_repo",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_operator_opcode_is_derived_from_tokens_when_helper_symbols_are_missing",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_real_libclang_backend_matches_json_oracle_for_core_c_fixture",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_real_libclang_header_cache_preserves_dual_run_and_worker_parity",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_real_libclang_backend_preserves_field_access_parity_without_opcode_helpers",
        "tests.test_live_libclang_smoke.LiveLibclangSmokeTest.test_live_libclang_init_covers_core_fact_and_relative_shapes",
        "tests.test_initializer_toolchain.InitializerToolchainTest.test_libclang_compile_flags_absolutize_path_arguments",
        "tests.test_initializer_compile_database.InitializerCompileDatabaseTest.test_per_file_arguments_are_sanitized_and_ordered_after_global_args",
        "tests.test_initializer_compile_database.InitializerCompileDatabaseTest.test_compile_database_ast_invocation_runs_from_entry_directory_for_relative_includes",
        "tests.test_initializer_compile_database.InitializerCompileDatabaseTest.test_compile_database_limits_sources_to_indexed_entries",
        "tests.test_initializer_compile_database.InitializerCompileDatabaseTest.test_malformed_compile_database_entries_fail_closed",
        "tests.test_code_extractor_parallel.CodeExtractorParallelTest.test_parallel_workers_merge_out_of_order_results_by_source_path_and_resolve_calls",
        "tests.test_code_extractor_parallel.CodeExtractorParallelTest.test_parallel_workers_keep_recoverable_file_errors_isolated",
        "tests.test_code_extractor_parallel.CodeExtractorParallelTest.test_map_reduce_staging_gc_removes_only_stale_initializer_runs",
    ],
    "path safety and exception branches": [
        "tests.test_initializer_path_safety.InitializerPathSafetyTest.test_source_root_escape_is_rejected_without_creating_outside_outputs",
        "tests.test_initializer_path_safety.InitializerPathSafetyTest.test_invalid_source_root_profile_and_log_enabled_are_structured_errors",
        "tests.test_initializer_path_safety.InitializerPathSafetyTest.test_compile_database_malformed_and_cipher_escape_are_reported",
        "tests.test_initializer_path_safety.InitializerPathSafetyTest.test_file_ast_warning_is_structured_without_source_leak",
    ],
    "observability and views": [
        "tests.test_initializer_observability.InitializerObservabilityTest.test_success_events_are_visible_in_log_view_with_counts",
        "tests.test_initializer_observability.InitializerObservabilityTest.test_init_stage_events_are_stable_sanitized_and_use_reduce_accumulator",
        "tests.test_initializer_observability.InitializerObservabilityTest.test_failure_events_have_stable_error_codes_without_traceback_or_absolute_paths",
        "tests.test_initializer_observability.InitializerObservabilityTest.test_log_write_failure_does_not_break_initializer_main_flow",
    ],
    "performance and smallness": [
        "tests.test_initializer_performance.InitializerPerformanceTest.test_memory_budget_formula_tracks_single_file_window_fact_buffer_and_margin",
        "tests.test_initializer_performance.InitializerPerformanceTest.test_small_medium_large_unit_workloads_stay_within_memory_budgets",
    ],
}


class InitializerCoverageMatrixTest(unittest.TestCase):
    def test_initializer_design_matrix_has_behavior_tests_for_every_requirement(self):
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

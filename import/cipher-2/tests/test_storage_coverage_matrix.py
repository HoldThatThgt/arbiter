import importlib
import unittest


EXPECTED_TESTS = {
    "fact record schema and stored line": [
        "tests.test_storage_fact_record.StorageFactRecordTest.test_fact_record_validates_required_fields_and_round_trips_payload",
        "tests.test_storage_fact_record.StorageFactRecordTest.test_fact_record_rejects_missing_or_invalid_required_fields",
        "tests.test_storage_fact_record.StorageFactRecordTest.test_fact_record_rejects_non_json_and_oversized_payload",
        "tests.test_storage_fact_record.StorageFactRecordTest.test_stored_fact_line_derives_fact_kind_and_payload_hash",
        "tests.test_storage_fact_record.StorageFactRecordTest.test_stored_fact_line_defaults_fact_kind_to_fact",
        "tests.test_storage_fact_record.StorageFactRecordTest.test_hot_fact_and_relative_records_are_frozen_slotted",
    ],
    "relative record schema and stored line": [
        "tests.test_storage_relative_record.StorageRelativeRecordTest.test_relative_condition_validates_and_round_trips",
        "tests.test_storage_relative_record.StorageRelativeRecordTest.test_fact_relative_validates_and_round_trips_payload",
        "tests.test_storage_relative_record.StorageRelativeRecordTest.test_stored_relative_line_hashes_and_restores_relative",
        "tests.test_storage_relative_record.StorageRelativeRecordTest.test_fact_relative_rejects_invalid_fields",
        "tests.test_storage_relative_record.StorageRelativeRecordTest.test_field_access_relation_kinds_are_supported",
        "tests.test_storage_relative_record.StorageRelativeRecordTest.test_condition_rejects_invalid_shape_and_size",
        "tests.test_storage_relative_record.StorageRelativeRecordTest.test_relative_rejects_non_json_and_oversized_payload",
    ],
    "snapshot replace read search stats": [
        "tests.test_storage_file_store.StorageFileStoreTest.test_empty_store_iter_get_search_and_stats_are_empty",
        "tests.test_storage_file_store.StorageFileStoreTest.test_replace_facts_writes_v5_gzip_snapshot_current_manifest_stats_index_and_facts",
        "tests.test_storage_file_store.StorageFileStoreTest.test_iter_get_search_and_stats_use_current_snapshot",
        "tests.test_storage_file_store.StorageFileStoreTest.test_first_query_uses_persistent_index_without_memory_rebuild",
        "tests.test_storage_file_store.StorageFileStoreTest.test_same_content_reuses_snapshot_and_keeps_current_pointer",
        "tests.test_storage_file_store.StorageFileStoreTest.test_replace_rejects_duplicate_object_ids",
        "tests.test_storage_file_store.StorageFileStoreTest.test_replace_snapshot_sorted_unique_bypasses_re_sort_and_validates_order",
        "tests.test_storage_file_store.StorageFileStoreTest.test_preencoded_sorted_unique_path_writes_same_snapshot_without_reencoding_records",
        "tests.test_storage_file_store.StorageFileStoreTest.test_read_only_replace_and_invalid_mode_are_rejected",
        "tests.test_storage_file_store.StorageFileStoreTest.test_search_rejects_invalid_query_and_limit",
        "tests.test_storage_file_store.StorageFileStoreTest.test_search_splits_terms_and_requires_all_terms",
        "tests.test_storage_file_store.StorageFileStoreTest.test_search_promotes_exact_type_and_function_over_same_named_fields",
        "tests.test_storage_file_store.StorageFileStoreTest.test_search_keeps_exact_field_reachable_when_high_rank_kinds_share_name",
    ],
    "relative snapshot read query stats": [
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_replace_snapshot_writes_v5_gzip_relatives_manifest_stats_index_and_files",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_replace_snapshot_sorted_unique_validates_relative_endpoints",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_iter_relatives_stats_and_relations_query_current_snapshot",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_single_text_fallback_anchor_needs_refinement",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_unique_exact_name_anchor_ignores_text_fallback_candidates",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_depth_two_callees_returns_shortest_hops_and_skips_root_cycle",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_reachable_returns_shortest_path_and_depth_incomplete",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_reachable_path_nodes_preserve_conditions",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_traverses_dispatch_edges_via_assigned_targets",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_invalid_depth_needs_refinement_without_exception",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_hard_error_messages_include_next_actions",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_one_hop_too_broad_is_complete_with_exact_total",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_search_frontier_budget_reports_incomplete_partial_answer",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_replace_snapshot_rejects_duplicate_relative_and_missing_endpoint",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relations_reject_invalid_args",
        "tests.test_storage_relative_store.StorageRelativeStoreTest.test_relation_kind_error_lists_supported_kinds",
        "tests.test_storage_relative_no_compat.StorageRelativeNoCompatTest.test_old_schema_v1_snapshot_is_not_compatible",
        "tests.test_storage_relative_no_compat.StorageRelativeNoCompatTest.test_missing_relatives_file_is_manifest_mismatch",
        "tests.test_storage_source_inventory.StorageSourceInventoryTest.test_replace_snapshot_writes_v5_gzip_source_inventory_index_and_stats",
        "tests.test_storage_source_inventory.StorageSourceInventoryTest.test_source_inventory_rejects_path_escape_and_bad_hash",
        "tests.test_incremental_overlay_view.IncrementalOverlayViewTest.test_fact_view_applies_source_tombstone_and_overlay_upsert",
        "tests.test_incremental_overlay_view.IncrementalOverlayViewTest.test_source_tombstone_hides_base_relatives_from_dirty_source",
        "tests.test_incremental_overlay_view.IncrementalOverlayViewTest.test_fact_view_relations_and_stats_use_complete_visible_fact_set",
        "tests.test_incremental_overlay_view.IncrementalOverlayViewTest.test_notify_file_changed_builds_overlay_and_records_incremental_log",
        "tests.test_incremental_overlay_view.IncrementalOverlayViewTest.test_compile_database_header_change_fans_out_to_dependent_translation_unit",
        "tests.test_incremental_overlay_view.IncrementalOverlayViewTest.test_standard_library_poller_observes_saved_file_and_publishes_overlay",
        "tests.test_incremental_overlay_view.IncrementalOverlayViewTest.test_overlay_endpoint_orphan_keeps_base_view_and_records_error",
        "tests.test_incremental_overlay_view.IncrementalOverlayViewTest.test_missing_changed_source_builds_tombstone_overlay",
    ],
    "path safety locking recovery": [
        "tests.test_storage_path_safety.StoragePathSafetyTest.test_write_outputs_stay_inside_target_cipher_directory",
        "tests.test_storage_path_safety.StoragePathSafetyTest.test_read_only_open_does_not_create_runtime_directories",
        "tests.test_storage_path_safety.StoragePathSafetyTest.test_symlink_escape_is_rejected",
        "tests.test_storage_path_safety.StoragePathSafetyTest.test_lock_busy_is_rejected_and_force_unlock_removes_stale_lock",
        "tests.test_storage_path_safety.StoragePathSafetyTest.test_force_unlock_keeps_live_lock",
    ],
    "corruption recovery": [
        "tests.test_storage_corruption.StorageCorruptionTest.test_malformed_gzip_facts_jsonl_is_snapshot_corrupt",
        "tests.test_storage_corruption.StorageCorruptionTest.test_facts_hash_mismatch_is_manifest_mismatch",
        "tests.test_storage_corruption.StorageCorruptionTest.test_cached_search_revalidates_modified_facts_file",
        "tests.test_storage_corruption.StorageCorruptionTest.test_v3_plaintext_snapshot_is_not_compatible",
        "tests.test_storage_corruption.StorageCorruptionTest.test_unsupported_schema_version_is_reported",
        "tests.test_storage_corruption.StorageCorruptionTest.test_current_pointer_to_missing_snapshot_is_reported",
        "tests.test_storage_corruption.StorageCorruptionTest.test_stats_mismatch_manifest_stats_differs_from_stats_json",
        "tests.test_storage_corruption.StorageCorruptionTest.test_stats_mismatch_manifest_bytes_differs_from_stats_bytes",
        "tests.test_storage_corruption.StorageCorruptionTest.test_stats_mismatch_actual_disk_bytes_differs_from_manifest",
        "tests.test_storage_corruption.StorageCorruptionTest.test_missing_persistent_read_index_is_manifest_mismatch_for_query",
        "tests.test_storage_corruption.StorageCorruptionTest.test_old_read_index_schema_version_is_manifest_mismatch_for_query",
        "tests.test_storage_corruption.StorageCorruptionTest.test_corrupt_persistent_read_index_is_snapshot_corrupt_for_query",
    ],
    "storage observability": [
        "tests.test_storage_observability.StorageObservabilityTest.test_replace_facts_writes_storage_write_event",
        "tests.test_storage_observability.StorageObservabilityTest.test_first_search_writes_storage_index_open_event",
        "tests.test_storage_observability.StorageObservabilityTest.test_idempotent_replace_writes_skipped_idempotent_outcome",
        "tests.test_storage_observability.StorageObservabilityTest.test_search_writes_query_observability_without_raw_payload",
        "tests.test_storage_observability.StorageObservabilityTest.test_search_failure_writes_storage_error_event",
        "tests.test_storage_observability.StorageObservabilityTest.test_log_write_failure_does_not_break_snapshot_and_is_exposed",
    ],
    "storage view model inputs": [
        "tests.test_storage_view_model.StorageViewModelInputTest.test_empty_stats_support_empty_view_state",
        "tests.test_storage_view_model.StorageViewModelInputTest.test_ready_stats_expose_core_view_fields",
        "tests.test_storage_view_model.StorageViewModelInputTest.test_log_degraded_stats_support_warning_view_state",
        "tests.test_storage_view_model.StorageViewModelInputTest.test_lock_state_supports_lock_warning_view_state",
        "tests.test_storage_view_model.StorageViewModelInputTest.test_corruption_error_is_structured_for_error_view_state",
    ],
    "performance and low limit": [
        "tests.test_storage_performance.StoragePerformanceTest.test_small_medium_large_workloads_stay_within_memory_budgets",
        "tests.test_storage_performance.StoragePerformanceTest.test_search_low_limit_does_not_materialize_extra_results",
    ],
}


class StorageCoverageMatrixTest(unittest.TestCase):
    def test_storage_design_matrix_has_behavior_tests_for_every_requirement(self):
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

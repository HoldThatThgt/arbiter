import importlib
import unittest


EXPECTED_TESTS = {
    "tool models and schemas": [
        "tests.test_mcp_tool_models.McpToolModelsTest.test_open_server_validates_target_repo_and_log_enabled",
        "tests.test_mcp_tool_models.McpToolModelsTest.test_list_tools_declares_only_search_detail_input_and_output_schema",
        "tests.test_mcp_tool_models.McpToolModelsTest.test_call_tool_rejects_unknown_tool_as_structured_mcp_error",
        "tests.test_mcp_tool_models.McpToolModelsTest.test_impact_is_not_exposed_as_mcp_tool",
        "tests.test_mcp_tool_models.McpToolModelsTest.test_relations_is_not_exposed_as_mcp_tool",
        "tests.test_mcp_tool_models.McpToolModelsTest.test_search_and_detail_reject_removed_scope_argument",
    ],
    "search detail behavior": [
        "tests.test_mcp_search_detail.McpSearchDetailTest.test_search_returns_fact_summaries_with_empty_query_object_id_order",
        "tests.test_mcp_search_detail.McpSearchDetailTest.test_search_tool_call_returns_structured_content_and_text_summary",
        "tests.test_mcp_search_detail.McpSearchDetailTest.test_search_exact_identifier_without_exact_object_name_reports_fallback_signal",
        "tests.test_mcp_search_detail.McpSearchDetailTest.test_search_multi_term_uses_and_semantics_order_independent",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_readers_file_filter_strips_endpoint_line_suffix",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_relation_search_too_broad_is_keyed_by_limit_not_fixed_threshold",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_callers_callees_and_caller_name_synonyms_are_deterministic",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_relation_search_transitive_uses_slim_rows_and_no_salience_duplicate",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_reachable_returns_path_complete_and_no_endpoint_rows",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_reachable_path_serializes_hop_conditions_and_no_hit_keeps_empty_path",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_reachable_path_can_cross_dispatch_edge",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_relation_search_invalid_depth_returns_refinement_response",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_relation_search_hard_error_tool_result_keeps_storage_guidance",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_unique_exact_function_anchor_ignores_substring_fallback_candidates",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_ambiguous_anchor_returns_deterministically_ordered_refinement",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_single_fuzzy_anchor_requires_refinement_instead_of_joining_wrong_fact",
        "tests.test_mcp_relation_search.McpRelationSearchTest.test_relation_search_sees_overlay_facts_and_relatives",
        "tests.test_mcp_search_detail.McpSearchDetailTest.test_detail_returns_payload_and_source_context_for_object_source_line",
        "tests.test_mcp_search_detail.McpSearchDetailTest.test_detail_tool_call_returns_not_found_error_result",
        "tests.test_incremental_mcp_view_state.IncrementalMcpViewStateTest.test_search_and_detail_include_base_view_state",
        "tests.test_incremental_mcp_view_state.IncrementalMcpViewStateTest.test_mcp_reads_injected_incremental_overlay_view",
        "tests.test_incremental_mcp_view_state.IncrementalMcpViewStateTest.test_open_mcp_server_reconciles_changed_sources_before_queries",
    ],
    "internal relation preview": [
        "tests.test_mcp_relations.McpRelationsTest.test_relations_tool_call_is_not_public_mcp_interface",
        "tests.test_mcp_relations.McpRelationsTest.test_detail_preview_exposes_bounded_internal_relation_audit",
        "tests.test_mcp_relations.McpRelationsTest.test_detail_preview_buckets_relatives_by_direction_and_kind",
        "tests.test_mcp_relations.McpRelationsTest.test_detail_preview_high_fan_in_field_readers_keep_counts_and_diversity",
        "tests.test_mcp_relations.McpRelationsTest.test_detail_preview_rolls_up_call_sites_and_keeps_conditions",
        "tests.test_mcp_relations.McpRelationsTest.test_detail_preview_uses_source_diversity_and_missing_endpoint_defaults",
        "tests.test_mcp_relations.McpRelationsTest.test_detail_preview_missing_endpoint_profile_is_null",
        "tests.test_mcp_relations.McpRelationsTest.test_detail_preview_rollup_and_selection_are_identical_for_overlay_view",
    ],
    "stdio protocol": [
        "tests.test_mcp_stdio_protocol.McpStdioProtocolTest.test_stdio_initialize_list_ping_and_search_call",
        "tests.test_mcp_stdio_protocol.McpStdioProtocolTest.test_stdio_protocol_errors_are_json_rpc_errors_not_tool_results",
    ],
    "response budgets": [
        "tests.test_mcp_response_budget.McpResponseBudgetTest.test_detail_budget_controls_payload_and_source_context_size",
        "tests.test_mcp_response_budget.McpResponseBudgetTest.test_detail_large_response_respects_declared_response_bytes",
        "tests.test_mcp_response_budget.McpResponseBudgetTest.test_search_payload_preview_is_bounded_and_marks_truncation",
    ],
    "observability and views": [
        "tests.test_mcp_observability.McpObservabilityTest.test_search_detail_and_errors_write_mcp_events_visible_in_log_view",
        "tests.test_mcp_observability.McpObservabilityTest.test_mcp_does_not_call_observe_batch_or_write_batch_summary",
        "tests.test_mcp_observability.McpObservabilityTest.test_log_disabled_suppresses_mcp_channel_side_effects",
    ],
    "path safety": [
        "tests.test_mcp_path_safety.McpPathSafetyTest.test_unrecognized_source_format_is_warning_without_absolute_path_leak",
        "tests.test_mcp_path_safety.McpPathSafetyTest.test_source_path_escape_is_rejected_without_reading_outside_file",
        "tests.test_mcp_path_safety.McpPathSafetyTest.test_source_unreadable_reports_warning_and_no_traceback",
    ],
    "performance and smallness": [
        "tests.test_mcp_performance.McpPerformanceTest.test_small_medium_large_unit_workloads_stay_within_memory_budgets",
        "tests.test_mcp_performance.McpPerformanceTest.test_low_limit_search_keeps_response_small",
    ],
}


class McpCoverageMatrixTest(unittest.TestCase):
    def test_mcp_design_matrix_has_behavior_tests_for_every_requirement(self):
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

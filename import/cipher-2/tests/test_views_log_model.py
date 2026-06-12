import tempfile
import unittest
from pathlib import Path

from cipher2.tools.log import LogEvent, open_log
from cipher2.tools.views import LogEventRow, LogViewModel, build_overview


class ViewsLogModelTest(unittest.TestCase):
    def test_empty_log_model_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            overview = build_overview(Path(tmp), include_sections=["log"])

            self.assertEqual(overview.state, "empty")
            self.assertIsInstance(overview.log, LogViewModel)
            self.assertEqual(overview.log.state, "empty")
            self.assertEqual(overview.log.total_events, 0)
            self.assertEqual(overview.log.recent_events, [])
            self.assertEqual(overview.log.slow_events, [])
            self.assertEqual(overview.log.init_stage_timings, [])

    def test_log_model_renders_recent_slow_rows_and_top_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(
                LogEvent(
                    event_name="storage.search",
                    channel="storage",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    duration_ms=2.0,
                    summary=None,
                    counts={"matched_count": 3, "limit": 20},
                    payload={"query_kind": "substring", "query_preview": "parse config", "limit": 20},
                )
            )
            log.write_event(
                LogEvent(
                    event_name="storage.write",
                    channel="storage",
                    timestamp="2026-05-25T10:01:00.000000Z",
                    duration_ms=5.0,
                    summary="stored facts",
                    subject_id="sha256-abc",
                    counts={"fact_count": 8},
                    payload={"snapshot_id": "sha256-abc", "bytes_written": 123},
                )
            )
            log.write_event(
                LogEvent(
                    event_name="storage.error",
                    channel="storage",
                    timestamp="2026-05-25T10:02:00.000000Z",
                    status="error",
                    error_code="invalid_limit",
                    duration_ms=1.0,
                    payload={"operation": "search", "outcome": "failed"},
                )
            )

            overview = build_overview(target, include_sections=["log"], top_n=2)

            self.assertEqual(overview.state, "error")
            self.assertEqual(overview.log.state, "error")
            self.assertEqual(overview.log.total_events, 3)
            self.assertEqual(overview.log.top_event_names, [("storage.error", 1), ("storage.search", 1)])
            self.assertEqual([row.label for row in overview.log.recent_events], ["storage.error", "storage.write", "storage.search"])
            self.assertEqual([row.label for row in overview.log.slow_events], ["storage.write", "storage.search", "storage.error"])
            row = overview.log.recent_events[-1]
            self.assertIsInstance(row, LogEventRow)
            self.assertEqual(row.summary, "storage.search")
            self.assertIn("matched_count=3", row.detail)
            self.assertIn(("query_kind", "substring"), row.fields)
            self.assertNotIn("payload", row.detail)
            self.assertLessEqual(len(row.fields), 16)

    def test_log_model_exposes_latest_init_stage_timings_outside_recent_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            stages = (
                "collect",
                "extract",
                "reduce",
                "resolve",
                "relative_merge",
                "snapshot_write",
                "read_index",
            )
            for index, stage in enumerate(stages):
                log.write_event(
                    LogEvent(
                        event_name="init.stage",
                        channel="initializer",
                        timestamp=f"2026-05-25T10:00:{index:02d}.000000Z",
                        duration_ms=float(index + 1),
                        counts={"source_count": index + 1},
                        payload={
                            "operation": "initialize_repository",
                            "outcome": "stage_completed",
                            "stage": stage,
                            "stage_duration_ms": index + 1,
                        },
                    )
                )
            for index in range(25):
                log.write_event(
                    LogEvent(
                        event_name="storage.search",
                        channel="storage",
                        timestamp=f"2026-05-25T10:01:{index:02d}.000000Z",
                        counts={"matched_count": index},
                        payload={"operation": "search", "outcome": "ok"},
                    )
                )

            overview = build_overview(target, include_sections=["log"])

            self.assertNotIn("init.stage", [row.label for row in overview.log.recent_events])
            self.assertEqual([stage.stage for stage in overview.log.init_stage_timings], list(stages))
            self.assertEqual(overview.log.init_stage_timings[-1].duration_ms, 7.0)

    def test_log_warning_state_for_malformed_lines_and_warning_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(
                LogEvent(
                    event_name="storage.search",
                    channel="storage",
                    status="warning",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    payload={"operation": "search"},
                )
            )
            with (target / ".cipher" / "log" / "storage.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("{bad json}\n")

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.state, "warning")
            self.assertEqual(overview.log.state, "warning")
            self.assertEqual(overview.log.malformed_lines, 1)
            self.assertEqual(overview.log.events_by_status["warning"], 1)

    def test_log_model_exposes_toolchain_and_file_warning_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(
                LogEvent(
                    event_name="extractor.code.toolchain",
                    channel="initializer",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    payload={
                        "clang_vendor": "apple",
                        "clang_version": "21.0.0",
                        "ast_json_supported": True,
                        "type_driven_ast": True,
                        "loc_file_supported": True,
                        "call_reference_supported": True,
                        "member_reference_supported": True,
                        "qual_type_supported": True,
                        "gcc_required": False,
                        "gcc_checked": False,
                    },
                )
            )
            log.write_event(
                LogEvent(
                    event_name="extractor.code.file",
                    channel="initializer",
                    timestamp="2026-05-25T10:01:00.000000Z",
                    status="warning",
                    error_code="clang_ast_partial",
                    counts={
                        "fact_count": 3,
                        "relative_count": 2,
                        "conditional_relative_count": 0,
                        "field_read_count": 1,
                        "field_write_count": 0,
                        "typed_member_expr_count": 1,
                        "typed_call_expr_count": 1,
                        "source_from_loc_file_count": 2,
                        "source_fallback_count": 2,
                        "unresolved_call_count": 1,
                        "field_owner_count": 1,
                        "record_owner_count": 3,
                        "anonymous_record_count": 1,
                        "synthetic_type_fact_count": 1,
                        "field_decl_count": 4,
                        "field_fact_count": 4,
                        "field_decl_without_fact_count": 0,
                        "wrapped_member_expr_count": 3,
                        "macro_wrapped_member_expr_count": 1,
                        "bitwise_member_expr_count": 1,
                        "compound_field_access_count": 1,
                        "field_access_scan_truncated_count": 0,
                        "field_access_resolved_count": 6,
                        "field_access_unresolved_count": 0,
                        "function_pointer_slot_count": 1,
                        "function_pointer_assignment_count": 4,
                        "function_pointer_dispatch_count": 3,
                        "macro_direct_call_count": 1,
                        "unresolved_dispatch_slot_count": 0,
                        "unresolved_dispatch_function_count": 0,
                        "compile_command_hit_count": 0,
                        "compile_command_miss_count": 0,
                        "compile_command_argument_count": 0,
                        "compile_command_stripped_argument_count": 0,
                        "partial_ast_count": 1,
                        "warning_count": 1,
                    },
                    payload={
                        "operation": "extract_file",
                        "outcome": "extracted_partial",
                        "source_kind": "c_source",
                        "diagnostic_kind": "partial_ast",
                        "partial_ast_count": 1,
                    },
                )
            )

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.log.toolchain_status, "ok")
            self.assertEqual(overview.log.clang_vendor, "apple")
            self.assertEqual(overview.log.clang_version, "21.0.0")
            self.assertEqual(overview.log.type_driven_ast, True)
            self.assertEqual(overview.log.loc_file_supported, True)
            self.assertEqual(overview.log.call_reference_supported, True)
            self.assertEqual(overview.log.member_reference_supported, True)
            self.assertEqual(overview.log.qual_type_supported, True)
            self.assertEqual(overview.log.gcc_required, False)
            self.assertEqual(overview.log.gcc_checked, False)
            self.assertEqual(overview.log.source_fallback_count, 2)
            self.assertEqual(overview.log.unresolved_call_count, 1)
            self.assertEqual(overview.log.partial_ast_count, 1)
            self.assertEqual(overview.log.record_owner_count, 3)
            self.assertEqual(overview.log.anonymous_record_count, 1)
            self.assertEqual(overview.log.synthetic_type_fact_count, 1)
            self.assertEqual(overview.log.field_decl_count, 4)
            self.assertEqual(overview.log.field_fact_count, 4)
            self.assertEqual(overview.log.field_decl_without_fact_count, 0)
            self.assertEqual(overview.log.wrapped_member_expr_count, 3)
            self.assertEqual(overview.log.macro_wrapped_member_expr_count, 1)
            self.assertEqual(overview.log.bitwise_member_expr_count, 1)
            self.assertEqual(overview.log.compound_field_access_count, 1)
            self.assertEqual(overview.log.field_access_scan_truncated_count, 0)
            self.assertEqual(overview.log.field_access_resolved_count, 6)
            self.assertEqual(overview.log.field_access_unresolved_count, 0)
            self.assertEqual(overview.log.function_pointer_slot_count, 1)
            self.assertEqual(overview.log.function_pointer_assignment_count, 4)
            self.assertEqual(overview.log.function_pointer_dispatch_count, 3)
            self.assertEqual(overview.log.macro_direct_call_count, 1)
            self.assertEqual(overview.log.unresolved_dispatch_slot_count, 0)
            self.assertEqual(overview.log.unresolved_dispatch_function_count, 0)
            self.assertEqual(overview.log.latest_toolchain_error, "clang_ast_partial")
            warning_row = next(row for row in overview.log.recent_events if row.label == "extractor.code.file")
            self.assertEqual(warning_row.status, "warning")
            self.assertEqual(warning_row.error_code, "clang_ast_partial")
            self.assertIn(("outcome", "extracted_partial"), warning_row.fields)
            self.assertIn(("diagnostic_kind", "partial_ast"), warning_row.fields)
            self.assertIn(("partial_ast_count", "1"), warning_row.fields)

    def test_log_model_exposes_missing_type_driven_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_log(target).write_event(
                LogEvent(
                    event_name="extractor.code.toolchain",
                    channel="initializer",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    status="error",
                    error_code="clang_capability_failed",
                    payload={
                        "operation": "toolchain_probe",
                        "outcome": "failed",
                        "error_code": "clang_capability_failed",
                        "missing_evidence": "loc.file,call_reference",
                    },
                )
            )

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.log.latest_toolchain_error, "clang_capability_failed")
            self.assertEqual(overview.log.latest_missing_evidence, "loc.file,call_reference")

    def test_log_model_warns_on_field_coverage_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_log(target).write_event(
                LogEvent(
                    event_name="extractor.code.file",
                    channel="initializer",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    counts={
                        "field_decl_without_fact_count": 1,
                        "field_access_unresolved_count": 2,
                    },
                    payload={"operation": "extract_file", "outcome": "extracted"},
                )
            )

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.log.state, "warning")
            self.assertEqual(overview.state, "warning")
            self.assertEqual(overview.log.field_decl_without_fact_count, 1)
            self.assertEqual(overview.log.field_access_unresolved_count, 2)

    def test_log_model_warns_on_dispatch_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_log(target).write_event(
                LogEvent(
                    event_name="extractor.code.file",
                    channel="initializer",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    counts={
                        "unresolved_dispatch_slot_count": 1,
                        "unresolved_dispatch_function_count": 2,
                    },
                    payload={"operation": "extract_file", "outcome": "extracted"},
                )
            )

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.log.state, "warning")
            self.assertEqual(overview.state, "warning")
            self.assertEqual(overview.log.unresolved_dispatch_slot_count, 1)
            self.assertEqual(overview.log.unresolved_dispatch_function_count, 2)

    def test_log_model_exposes_mcp_relative_preview_quality_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_log(target).write_event(
                LogEvent(
                    event_name="mcp.detail",
                    channel="mcp",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    counts={
                        "relative_rollup_group_count": 3,
                        "relative_collapsed_instance_count": 2,
                        "relative_preview_source_file_count": 4,
                        "relative_diversity_bucket_count": 1,
                        "response_bytes": 4096,
                        "response_bytes_limit": 8192,
                        "response_truncated_count": 1,
                        "flat_relative_count": 8,
                        "flat_relative_dropped_count": 37,
                        "bucket_relative_dropped_count": 5,
                        "source_context_line_dropped_count": 6,
                        "payload_field_dropped_count": 2,
                    },
                    payload={"operation": "detail", "outcome": "read", "budget": "normal"},
                )
            )

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.log.state, "ready")
            self.assertEqual(overview.state, "ready")
            self.assertEqual(overview.log.relative_rollup_group_count, 3)
            self.assertEqual(overview.log.relative_collapsed_instance_count, 2)
            self.assertEqual(overview.log.relative_preview_source_file_count, 4)
            self.assertEqual(overview.log.relative_diversity_bucket_count, 1)
            self.assertEqual(overview.log.response_bytes, 4096)
            self.assertEqual(overview.log.response_bytes_limit, 8192)
            self.assertEqual(overview.log.response_truncated_count, 1)
            self.assertEqual(overview.log.flat_relative_count, 8)
            self.assertEqual(overview.log.flat_relative_dropped_count, 37)
            self.assertEqual(overview.log.bucket_relative_dropped_count, 5)
            self.assertEqual(overview.log.source_context_line_dropped_count, 6)
            self.assertEqual(overview.log.payload_field_dropped_count, 2)

    def test_log_model_warns_on_compile_database_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(
                LogEvent(
                    event_name="extractor.code.compile_database",
                    channel="initializer",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    counts={
                        "compile_command_entry_count": 3,
                        "compile_command_indexed_source_count": 1,
                        "compile_command_duplicate_source_count": 1,
                        "compile_command_ignored_outside_repo_count": 1,
                        "compile_command_stripped_argument_count": 4,
                    },
                    payload={"operation": "compile_database_index", "outcome": "indexed"},
                )
            )
            log.write_event(
                LogEvent(
                    event_name="extractor.code.file",
                    channel="initializer",
                    timestamp="2026-05-25T10:01:00.000000Z",
                    counts={
                        "compile_command_hit_count": 1,
                        "compile_command_miss_count": 1,
                        "compile_command_argument_count": 3,
                        "compile_command_stripped_argument_count": 4,
                    },
                    payload={"operation": "extract_file", "outcome": "extracted"},
                )
            )

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.log.state, "warning")
            self.assertEqual(overview.state, "warning")
            self.assertEqual(overview.log.compile_database_configured, True)
            self.assertEqual(overview.log.compile_command_hit_count, 1)
            self.assertEqual(overview.log.compile_command_miss_count, 1)
            self.assertEqual(overview.log.compile_command_argument_count, 3)
            self.assertEqual(overview.log.compile_command_stripped_argument_count, 8)
            self.assertEqual(overview.log.compile_command_entry_count, 3)
            self.assertEqual(overview.log.compile_command_indexed_source_count, 1)
            self.assertEqual(overview.log.compile_command_duplicate_source_count, 1)
            self.assertEqual(overview.log.compile_command_ignored_outside_repo_count, 1)

    def test_log_model_exposes_direct_call_resolution_warning_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            counts = {
                "pending_call_count": 5,
                "resolved_call_count": 2,
                "external_unresolved_count": 1,
                "internal_unresolved_count": 1,
                "ambiguous_call_count": 1,
                "linkage_filtered_count": 1,
                "missing_caller_count": 0,
                "duplicate_relation_count": 0,
                "resolver_worker_count": 3,
                "pending_shard_count": 3,
                "function_index_entry_count": 11,
                "resolver_duration_ms": 42,
            }
            open_log(target).write_event(
                LogEvent(
                    event_name="extractor.code.direct_call_resolution",
                    channel="initializer",
                    status="warning",
                    counts=counts,
                    payload={
                        "operation": "resolve_pending_direct_calls",
                        "profile": "debug",
                        **counts,
                    },
                )
            )

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.log.state, "warning")
            self.assertEqual(overview.state, "warning")
            self.assertEqual(overview.log.direct_call_pending_count, 5)
            self.assertEqual(overview.log.direct_call_resolved_count, 2)
            self.assertEqual(overview.log.direct_call_external_unresolved_count, 1)
            self.assertEqual(overview.log.direct_call_internal_unresolved_count, 1)
            self.assertEqual(overview.log.direct_call_ambiguous_count, 1)
            self.assertEqual(overview.log.direct_call_linkage_filtered_count, 1)
            self.assertEqual(overview.log.direct_call_missing_caller_count, 0)
            self.assertEqual(overview.log.direct_call_duplicate_relation_count, 0)
            self.assertEqual(overview.log.direct_call_resolver_worker_count, 3)
            self.assertEqual(overview.log.direct_call_pending_shard_count, 3)
            self.assertEqual(overview.log.direct_call_function_index_entry_count, 11)
            self.assertEqual(overview.log.direct_call_resolver_duration_ms, 42)
            warning_row = next(row for row in overview.log.recent_events if row.label == "extractor.code.direct_call_resolution")
            self.assertIn(("operation", "resolve_pending_direct_calls"), warning_row.fields)

    def test_log_model_exposes_worker_pool_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(
                LogEvent(
                    event_name="extractor.code.file",
                    channel="initializer",
                    counts={
                        "header_decl_cache_hit_count": 5,
                        "header_decl_cache_miss_count": 2,
                        "header_decl_skipped_subtree_count": 5,
                        "header_decl_seed_count": 3,
                    },
                    payload={
                        "operation": "extract_file",
                        "outcome": "extracted",
                        "profile": "debug",
                    },
                )
            )
            log.write_event(
                LogEvent(
                    event_name="extractor.code.worker_pool",
                    channel="initializer",
                    status="warning",
                    counts={
                        "source_count": 4,
                        "worker_count": 2,
                        "successful_file_count": 3,
                        "skipped_file_count": 1,
                        "partial_ast_count": 1,
                        "warning_count": 1,
                        "header_decl_cache_entry_count": 7,
                        "map_output_segment_count": 9,
                        "map_output_bytes": 1234,
                        "stale_run_gc_count": 1,
                        "worker_timeout_count": 2,
                        "worker_restart_count": 3,
                        "worker_crash_count": 1,
                        "relative_map_input_count": 10,
                        "relative_map_written_count": 6,
                        "relative_map_skipped_exact_count": 4,
                        "relative_worker_duplicate_exact_count": 4,
                        "relative_worker_duplicate_conflict_count": 0,
                        "relative_worker_dedup_tracked_entry_count": 6,
                        "relative_worker_dedup_saturated_count": 1,
                        "fact_line_passthrough_count": 7,
                        "relative_line_passthrough_count": 5,
                        "fact_line_passthrough_bytes": 700,
                        "relative_line_passthrough_bytes": 500,
                        "fact_line_reencoded_count": 1,
                        "relative_line_reencoded_count": 0,
                        "fact_duplicate_exact_count": 2,
                        "fact_duplicate_merge_parse_count": 1,
                        "fact_duplicate_conflict_count": 0,
                        "relative_duplicate_exact_count": 3,
                        "relative_duplicate_conflict_count": 0,
                        "passthrough_ratio_percent": 92,
                    },
                    payload={
                        "operation": "parallel_extract",
                        "outcome": "warning",
                        "mode": "bounded_pool",
                        "max_unmerged": 2,
                        "profile": "debug",
                    },
                )
            )
            log.write_event(
                LogEvent(
                    event_name="extractor.code.relative_merge",
                    channel="initializer",
                    counts={
                        "relative_merge_input_count": 20,
                        "relative_merge_accepted_count": 12,
                        "relative_merge_duplicate_exact_count": 8,
                        "relative_merge_conflict_count": 0,
                        "relative_merge_segment_count": 4,
                        "relative_merge_input_bytes": 4096,
                        "relative_merge_index_bytes": 512,
                        "relative_merge_duration_ms": 17,
                        "relative_merge_full_parse_count": 0,
                        "relative_merge_max_heap_size": 4,
                        "relative_merge_fan_in": 4,
                        "relative_merge_pass_count": 1,
                        "relative_merge_peak_open_segment_count": 4,
                    },
                    payload={
                        "operation": "external_relative_merge",
                        "outcome": "merged",
                        "mode": "external_k_way",
                        "profile": "debug",
                    },
                )
            )

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.log.state, "warning")
            self.assertEqual(overview.log.extractor_worker_mode, "bounded_pool")
            self.assertEqual(overview.log.extractor_worker_count, 2)
            self.assertEqual(overview.log.extractor_worker_max_unmerged, 2)
            self.assertEqual(overview.log.extractor_worker_source_count, 4)
            self.assertEqual(overview.log.extractor_worker_successful_file_count, 3)
            self.assertEqual(overview.log.extractor_worker_skipped_file_count, 1)
            self.assertEqual(overview.log.extractor_worker_map_output_segment_count, 9)
            self.assertEqual(overview.log.extractor_worker_map_output_bytes, 1234)
            self.assertEqual(overview.log.extractor_worker_stale_run_gc_count, 1)
            self.assertEqual(overview.log.extractor_worker_timeout_count, 2)
            self.assertEqual(overview.log.extractor_worker_restart_count, 3)
            self.assertEqual(overview.log.extractor_worker_crash_count, 1)
            self.assertEqual(overview.log.relative_map_input_count, 10)
            self.assertEqual(overview.log.relative_map_written_count, 6)
            self.assertEqual(overview.log.relative_map_skipped_exact_count, 4)
            self.assertEqual(overview.log.relative_worker_duplicate_exact_count, 4)
            self.assertEqual(overview.log.relative_worker_duplicate_conflict_count, 0)
            self.assertEqual(overview.log.relative_worker_dedup_tracked_entry_count, 6)
            self.assertEqual(overview.log.relative_worker_dedup_saturated_count, 1)
            self.assertEqual(overview.log.fact_line_passthrough_count, 7)
            self.assertEqual(overview.log.relative_line_passthrough_count, 5)
            self.assertEqual(overview.log.fact_line_passthrough_bytes, 700)
            self.assertEqual(overview.log.relative_line_passthrough_bytes, 500)
            self.assertEqual(overview.log.fact_line_reencoded_count, 1)
            self.assertEqual(overview.log.relative_line_reencoded_count, 0)
            self.assertEqual(overview.log.fact_duplicate_exact_count, 2)
            self.assertEqual(overview.log.fact_duplicate_merge_parse_count, 1)
            self.assertEqual(overview.log.fact_duplicate_conflict_count, 0)
            self.assertEqual(overview.log.relative_duplicate_exact_count, 3)
            self.assertEqual(overview.log.relative_duplicate_conflict_count, 0)
            self.assertEqual(overview.log.relative_merge_input_count, 20)
            self.assertEqual(overview.log.relative_merge_accepted_count, 12)
            self.assertEqual(overview.log.relative_merge_duplicate_exact_count, 8)
            self.assertEqual(overview.log.relative_merge_conflict_count, 0)
            self.assertEqual(overview.log.relative_merge_segment_count, 4)
            self.assertEqual(overview.log.relative_merge_input_bytes, 4096)
            self.assertEqual(overview.log.relative_merge_index_bytes, 512)
            self.assertEqual(overview.log.relative_merge_duration_ms, 17)
            self.assertEqual(overview.log.relative_merge_full_parse_count, 0)
            self.assertEqual(overview.log.relative_merge_max_heap_size, 4)
            self.assertEqual(overview.log.relative_merge_fan_in, 4)
            self.assertEqual(overview.log.relative_merge_pass_count, 1)
            self.assertEqual(overview.log.relative_merge_peak_open_segment_count, 4)
            self.assertEqual(overview.log.passthrough_ratio_percent, 92)
            self.assertEqual(overview.log.header_decl_cache_entry_count, 7)
            self.assertEqual(overview.log.header_decl_cache_hit_count, 5)
            self.assertEqual(overview.log.header_decl_cache_miss_count, 2)
            self.assertEqual(overview.log.header_decl_skipped_subtree_count, 5)
            self.assertEqual(overview.log.header_decl_seed_count, 3)
            worker_row = next(row for row in overview.log.recent_events if row.label == "extractor.code.worker_pool")
            self.assertIn(("mode", "bounded_pool"), worker_row.fields)
            self.assertIn(("count.skipped_file_count", "1"), worker_row.fields)
            self.assertIn(("count.map_output_segment_count", "9"), worker_row.fields)
            self.assertIn(("count.header_decl_cache_entry_count", "7"), worker_row.fields)
            merge_row = next(row for row in overview.log.recent_events if row.label == "extractor.code.relative_merge")
            self.assertIn(("mode", "external_k_way"), merge_row.fields)
            self.assertIn(("count.relative_merge_full_parse_count", "0"), merge_row.fields)
            self.assertIn(("count.relative_merge_peak_open_segment_count", "4"), merge_row.fields)
            file_row = next(row for row in overview.log.recent_events if row.label == "extractor.code.file")
            self.assertIn(("count.header_decl_cache_hit_count", "5"), file_row.fields)

    def test_log_error_section_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "log").write_text("not a directory", encoding="utf-8")

            overview = build_overview(target, include_sections=["log"])

            self.assertEqual(overview.state, "error")
            self.assertIsNone(overview.log)
            self.assertEqual([(error.section, error.code) for error in overview.errors], [("log", "log_summary_failed")])


if __name__ == "__main__":
    unittest.main()

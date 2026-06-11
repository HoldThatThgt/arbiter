import tempfile
import unittest
from pathlib import Path

from cipher2.tools.log import LogEvent, open_log


class LogSummaryTest(unittest.TestCase):
    def test_summary_aggregates_counts_status_errors_and_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="storage.write",
                    channel="storage",
                    status="ok",
                    duration_ms=1.5,
                    counts={"fact_count": 2},
                    timestamp="2026-05-25T10:00:00.000000Z",
                )
            )
            log.write_event(
                LogEvent(
                    event_name="storage.error",
                    channel="storage",
                    status="error",
                    duration_ms=2.5,
                    error_code="invalid_limit",
                    counts={"fact_count": 3},
                    timestamp="2026-05-25T10:01:00.000000Z",
                )
            )

            summary = log.summarize(channel="storage")

            self.assertEqual(summary.total_events, 2)
            self.assertEqual(summary.events_by_channel, {"storage": 2})
            self.assertEqual(summary.events_by_name["storage.write"], 1)
            self.assertEqual(summary.events_by_status, {"ok": 1, "error": 1})
            self.assertEqual(summary.error_codes, {"invalid_limit": 1})
            self.assertEqual(summary.custom_counts["fact_count"], 5)
            self.assertEqual(summary.duration_ms_total, 4.0)
            self.assertGreater(summary.bytes_on_disk, 0)
            self.assertEqual(summary.latest_event_at, "2026-05-25T10:01:00.000000Z")
            self.assertEqual(summary.latest_error_code, "invalid_limit")

    def test_recent_and_slow_events_are_bounded_and_sorted_by_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            for index in range(25):
                log.write_event(
                    LogEvent(
                        event_name="storage.search",
                        channel="storage",
                        duration_ms=float(index % 5),
                        timestamp=f"2026-05-25T10:{index:02d}:00.000000Z",
                        summary=f"event-{index}",
                    )
                )

            summary = log.summarize(channel="storage")

            self.assertEqual(len(summary.recent_events), 20)
            self.assertEqual(summary.recent_events[0].summary, "event-5")
            self.assertEqual(summary.recent_events[-1].summary, "event-24")
            self.assertLessEqual(len(summary.slow_events), 20)
            slow_keys = [
                (event.duration_ms, event.timestamp)
                for event in summary.slow_events
            ]
            self.assertEqual(slow_keys, sorted(slow_keys, key=lambda item: (item[0], item[1]), reverse=True))

    def test_digest_counts_fields_limits_and_order_are_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            counts = {f"k{i:02d}": i for i in range(12)}
            payload = {
                "operation": "search",
                "outcome": "searched",
                "snapshot_id": "snap",
                "query_kind": "substring",
                "query_preview": "abc",
                "matched_count": 3,
                "limit": 20,
                "fact_count": 100,
                "bytes_written": 55,
                "latest_log_error_code": "none",
            }
            log.write_event(
                LogEvent(
                    event_name="storage.search",
                    channel="storage",
                    correlation_id="corr",
                    subject_id="subject",
                    duration_ms=7.0,
                    error_code=None,
                    counts=counts,
                    payload=payload,
                )
            )

            digest = log.summarize(channel="storage").recent_events[0]

            self.assertEqual(list(digest.counts), [f"k{i:02d}" for i in range(8)])
            self.assertLessEqual(len(digest.fields), 16)
            self.assertEqual(
                [name for name, _value in digest.fields[:4]],
                ["correlation_id", "subject_id", "duration_ms", "count.k00"],
            )

    def test_digest_includes_toolchain_capability_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="extractor.code.toolchain",
                    channel="initializer",
                    payload={
                        "clang_vendor": "apple",
                        "clang_version": "21.0.0",
                        "ast_json_supported": True,
                        "type_driven_ast": True,
                        "loc_file_supported": True,
                        "call_reference_supported": True,
                        "member_reference_supported": True,
                        "qual_type_supported": True,
                        "missing_evidence": "loc.file,call_reference",
                        "gcc_required": False,
                        "gcc_checked": False,
                        "warning_count": 0,
                    },
                )
            )

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("clang_vendor", "apple"), digest.fields)
            self.assertIn(("clang_version", "21.0.0"), digest.fields)
            self.assertIn(("ast_json_supported", "True"), digest.fields)
            self.assertIn(("type_driven_ast", "True"), digest.fields)
            self.assertIn(("loc_file_supported", "True"), digest.fields)
            self.assertIn(("call_reference_supported", "True"), digest.fields)
            self.assertIn(("member_reference_supported", "True"), digest.fields)
            self.assertIn(("qual_type_supported", "True"), digest.fields)
            self.assertIn(("missing_evidence", "loc.file,call_reference"), digest.fields)
            self.assertIn(("gcc_required", "False"), digest.fields)
            self.assertIn(("gcc_checked", "False"), digest.fields)

    def test_digest_includes_init_stage_fields_and_latest_stage_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="init.stage",
                    channel="initializer",
                    timestamp="2026-05-25T10:00:00.000000Z",
                    duration_ms=12.0,
                    counts={"source_count": 3, "worker_count": 2},
                    payload={
                        "operation": "initialize_repository",
                        "outcome": "stage_completed",
                        "stage": "extract",
                        "stage_duration_ms": 12,
                    },
                )
            )
            for index in range(25):
                log.write_event(
                    LogEvent(
                        event_name="storage.search",
                        channel="storage",
                        timestamp=f"2026-05-25T10:01:{index:02d}.000000Z",
                        payload={"operation": "search"},
                    )
                )

            summary = log.summarize()

            self.assertNotIn("init.stage", [event.event_name for event in summary.recent_events])
            self.assertEqual(len(summary.latest_init_stage_events), 1)
            digest = summary.latest_init_stage_events[0]
            self.assertEqual(digest.event_name, "init.stage")
            self.assertIn(("stage", "extract"), digest.fields)
            self.assertIn(("stage_duration_ms", "12"), digest.fields)

    def test_digest_includes_file_warning_diagnostic_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="extractor.code.file",
                    channel="initializer",
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
                        "source_fallback_count": 0,
                        "unresolved_call_count": 0,
                        "field_owner_count": 1,
                        "record_owner_count": 1,
                        "anonymous_record_count": 0,
                        "synthetic_type_fact_count": 0,
                        "field_decl_count": 1,
                        "field_fact_count": 1,
                        "field_decl_without_fact_count": 0,
                        "field_access_resolved_count": 1,
                        "field_access_unresolved_count": 0,
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
                        "diagnostic_kind": "partial_ast",
                        "partial_ast_count": 1,
                    },
                )
            )

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("diagnostic_kind", "partial_ast"), digest.fields)
            self.assertIn(("partial_ast_count", "1"), digest.fields)

    def test_digest_includes_field_coverage_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="extractor.code.file",
                    channel="initializer",
                    payload={
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
                    },
                )
            )

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("record_owner_count", "3"), digest.fields)
            self.assertIn(("anonymous_record_count", "1"), digest.fields)
            self.assertIn(("synthetic_type_fact_count", "1"), digest.fields)
            self.assertIn(("field_decl_count", "4"), digest.fields)
            self.assertIn(("field_fact_count", "4"), digest.fields)
            self.assertIn(("field_decl_without_fact_count", "0"), digest.fields)
            self.assertIn(("wrapped_member_expr_count", "3"), digest.fields)
            self.assertIn(("macro_wrapped_member_expr_count", "1"), digest.fields)
            self.assertIn(("bitwise_member_expr_count", "1"), digest.fields)
            self.assertIn(("compound_field_access_count", "1"), digest.fields)
            self.assertIn(("field_access_scan_truncated_count", "0"), digest.fields)
            self.assertIn(("field_access_resolved_count", "6"), digest.fields)
            self.assertIn(("field_access_unresolved_count", "0"), digest.fields)

    def test_digest_includes_function_pointer_dispatch_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="extractor.code.file",
                    channel="initializer",
                    payload={
                        "function_pointer_slot_count": 1,
                        "function_pointer_assignment_count": 4,
                        "function_pointer_dispatch_count": 3,
                        "macro_direct_call_count": 1,
                        "unresolved_dispatch_slot_count": 0,
                        "unresolved_dispatch_function_count": 0,
                    },
                )
            )

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("function_pointer_slot_count", "1"), digest.fields)
            self.assertIn(("function_pointer_assignment_count", "4"), digest.fields)
            self.assertIn(("function_pointer_dispatch_count", "3"), digest.fields)
            self.assertIn(("macro_direct_call_count", "1"), digest.fields)
            self.assertIn(("unresolved_dispatch_slot_count", "0"), digest.fields)
            self.assertIn(("unresolved_dispatch_function_count", "0"), digest.fields)

    def test_digest_includes_mcp_relative_preview_quality_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="mcp.detail",
                    channel="mcp",
                    counts={
                        **{f"filler_{index}": index for index in range(12)},
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
                    payload={
                        "operation": "detail",
                        "outcome": "read",
                        "budget": "normal",
                    },
                )
            )

            digest = log.summarize(channel="mcp").recent_events[0]

            self.assertIn(("count.relative_rollup_group_count", "3"), digest.fields)
            self.assertIn(("count.relative_collapsed_instance_count", "2"), digest.fields)
            self.assertIn(("count.relative_preview_source_file_count", "4"), digest.fields)
            self.assertIn(("count.relative_diversity_bucket_count", "1"), digest.fields)
            self.assertIn(("count.response_bytes", "4096"), digest.fields)
            self.assertIn(("count.response_bytes_limit", "8192"), digest.fields)
            self.assertIn(("count.response_truncated_count", "1"), digest.fields)
            self.assertIn(("count.flat_relative_count", "8"), digest.fields)

    def test_digest_includes_compile_database_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="extractor.code.compile_database",
                    channel="initializer",
                    counts={
                        "compile_command_entry_count": 3,
                        "compile_command_indexed_source_count": 1,
                        "compile_command_duplicate_source_count": 1,
                        "compile_command_ignored_outside_repo_count": 1,
                        "compile_command_stripped_argument_count": 4,
                    },
                    payload={
                        "operation": "compile_database_index",
                        "outcome": "indexed",
                        "compile_command_entry_count": 3,
                        "compile_command_indexed_source_count": 1,
                        "compile_command_duplicate_source_count": 1,
                        "compile_command_ignored_outside_repo_count": 1,
                        "compile_command_stripped_argument_count": 4,
                    },
                )
            )

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("compile_command_entry_count", "3"), digest.fields)
            self.assertIn(("compile_command_indexed_source_count", "1"), digest.fields)
            self.assertIn(("compile_command_duplicate_source_count", "1"), digest.fields)
            self.assertIn(("compile_command_ignored_outside_repo_count", "1"), digest.fields)
            self.assertIn(("compile_command_stripped_argument_count", "4"), digest.fields)

    def test_digest_includes_direct_call_resolution_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
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
            log.write_event(
                LogEvent(
                    event_name="extractor.code.direct_call_resolution",
                    channel="initializer",
                    counts=counts,
                    payload={
                        "operation": "resolve_pending_direct_calls",
                        "profile": "debug",
                        **counts,
                    },
                )
            )

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("count.pending_call_count", "5"), digest.fields)
            self.assertIn(("count.resolved_call_count", "2"), digest.fields)
            self.assertIn(("count.resolver_worker_count", "3"), digest.fields)
            self.assertIn(("count.pending_shard_count", "3"), digest.fields)
            self.assertIn(("pending_call_count", "5"), digest.fields)
            self.assertIn(("resolved_call_count", "2"), digest.fields)
            self.assertIn(("operation", "resolve_pending_direct_calls"), digest.fields)

    def test_digest_includes_worker_pool_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="extractor.code.worker_pool",
                    channel="initializer",
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

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("count.worker_count", "2"), digest.fields)
            self.assertIn(("count.successful_file_count", "3"), digest.fields)
            self.assertIn(("count.skipped_file_count", "1"), digest.fields)
            self.assertIn(("count.map_output_segment_count", "9"), digest.fields)
            self.assertIn(("count.header_decl_cache_entry_count", "7"), digest.fields)
            self.assertIn(("mode", "bounded_pool"), digest.fields)
            self.assertIn(("max_unmerged", "2"), digest.fields)
            self.assertIn(("operation", "parallel_extract"), digest.fields)

    def test_digest_includes_worker_recovery_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="extractor.code.worker_pool",
                    channel="initializer",
                    counts={
                        "worker_timeout_count": 2,
                        "worker_restart_count": 3,
                        "worker_crash_count": 1,
                    },
                    payload={
                        "operation": "parallel_extract",
                        "outcome": "warning",
                        "mode": "bounded_pool",
                    },
                )
            )

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("count.worker_timeout_count", "2"), digest.fields)
            self.assertIn(("count.worker_restart_count", "3"), digest.fields)
            self.assertIn(("count.worker_crash_count", "1"), digest.fields)
            self.assertIn(("mode", "bounded_pool"), digest.fields)
            self.assertIn(("operation", "parallel_extract"), digest.fields)

    def test_digest_includes_worker_relative_dedup_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
            log.write_event(
                LogEvent(
                    event_name="extractor.code.worker_pool",
                    channel="initializer",
                    counts={
                        "relative_map_input_count": 10,
                        "relative_map_written_count": 6,
                        "relative_map_skipped_exact_count": 4,
                        "relative_worker_duplicate_exact_count": 4,
                        "relative_worker_duplicate_conflict_count": 0,
                        "relative_worker_dedup_tracked_entry_count": 6,
                        "relative_worker_dedup_saturated_count": 1,
                    },
                    payload={
                        "operation": "parallel_extract",
                        "outcome": "completed",
                        "mode": "serial",
                        "max_unmerged": 1,
                        "profile": "debug",
                    },
                )
            )

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("count.relative_map_input_count", "10"), digest.fields)
            self.assertIn(("count.relative_map_written_count", "6"), digest.fields)
            self.assertIn(("count.relative_map_skipped_exact_count", "4"), digest.fields)
            self.assertIn(("count.relative_worker_duplicate_exact_count", "4"), digest.fields)
            self.assertIn(("count.relative_worker_dedup_tracked_entry_count", "6"), digest.fields)
            self.assertIn(("count.relative_worker_dedup_saturated_count", "1"), digest.fields)
            self.assertIn(("mode", "serial"), digest.fields)
            self.assertIn(("operation", "parallel_extract"), digest.fields)

    def test_digest_includes_relative_merge_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = open_log(Path(tmp))
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

            digest = log.summarize(channel="initializer").recent_events[0]

            self.assertIn(("count.relative_merge_input_count", "20"), digest.fields)
            self.assertIn(("count.relative_merge_accepted_count", "12"), digest.fields)
            self.assertIn(("count.relative_merge_duplicate_exact_count", "8"), digest.fields)
            self.assertIn(("count.relative_merge_full_parse_count", "0"), digest.fields)
            self.assertIn(("count.relative_merge_fan_in", "4"), digest.fields)
            self.assertIn(("count.relative_merge_pass_count", "1"), digest.fields)
            self.assertIn(("count.relative_merge_peak_open_segment_count", "4"), digest.fields)
            self.assertIn(("mode", "external_k_way"), digest.fields)
            self.assertIn(("operation", "external_relative_merge"), digest.fields)

    def test_summary_records_redaction_truncation_and_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            log.write_event(
                LogEvent(
                    event_name="storage.write",
                    channel="storage",
                    payload={"token": "abc", "large": "x" * 900},
                )
            )
            with (target / ".cipher" / "log" / "storage.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("{bad json}\n")

            summary = log.summarize(channel="storage")

            self.assertEqual(summary.malformed_lines, 1)
            self.assertGreaterEqual(summary.dropped_field_count, 1)
            self.assertGreaterEqual(summary.truncated_field_count, 1)


if __name__ == "__main__":
    unittest.main()

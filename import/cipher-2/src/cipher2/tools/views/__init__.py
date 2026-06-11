"""Read-only view models for cipher-2 tools state."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cipher2.incremental import IncrementalError, read_incremental_status
from cipher2.storage import StorageError, open_fact_store
from cipher2.tools.log import LogError, LogEvent, LogEventDigest, LogSummary, open_log


TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
ALLOWED_SECTIONS = {"storage", "log", "incremental"}
INIT_STAGE_ORDER = (
    "collect",
    "extract",
    "reduce",
    "resolve",
    "relative_merge",
    "snapshot_write",
    "read_index",
)


@dataclass(frozen=True)
class ViewBuildRequest:
    target_repo: Path
    include_sections: List[str]
    top_n: int
    since: Optional[str] = None
    until: Optional[str] = None


@dataclass(frozen=True)
class StorageViewModel:
    state: str
    snapshot_id: Optional[str]
    snapshot_format: Optional[str]
    compression: Optional[str]
    total_facts: int
    total_relatives: int
    total_sources: int
    fact_kinds: Dict[str, int]
    relation_kinds: Dict[str, int]
    field_read_count: int
    field_write_count: int
    conditional_relative_count: int
    orphan_relative_count: int
    profiles: Dict[str, int]
    source_files: Dict[str, int]
    snapshot_count: int
    bytes_on_disk: int
    bytes_on_disk_total: int
    uncompressed_bytes: int
    compression_ratio: float
    storage_overhead_ratio: float
    file_bytes: Dict[str, Dict[str, object]]
    read_index_state: str
    read_index_bytes: int
    read_index_schema_version: Optional[int]
    read_index_codec: Optional[str]
    lock_state: str
    search_count: int
    relations_count: int
    error_count: int
    log_write_failures: int
    latest_error_code: Optional[str]
    latest_log_error_code: Optional[str]


@dataclass(frozen=True)
class LogEventRow:
    timestamp: str
    label: str
    status: str
    duration_ms: Optional[float]
    summary: str
    detail: str
    fields: List[Tuple[str, str]]
    error_code: Optional[str]


@dataclass(frozen=True)
class InitStageView:
    stage: str
    status: str
    duration_ms: float
    counts: Dict[str, int]


@dataclass(frozen=True)
class LogViewModel:
    state: str
    total_events: int
    events_by_channel: Dict[str, int]
    events_by_status: Dict[str, int]
    top_event_names: List[Tuple[str, int]]
    recent_events: List[LogEventRow]
    slow_events: List[LogEventRow]
    error_codes: Dict[str, int]
    init_stage_timings: List[InitStageView]
    duration_ms_total: float
    malformed_lines: int
    bytes_on_disk: int
    latest_event_at: Optional[str]
    latest_error_code: Optional[str]
    redaction_summary: Dict[str, int]
    dropped_event_count: int
    toolchain_status: Optional[str]
    toolchain_backend: Optional[str]
    clang_vendor: Optional[str]
    clang_version: Optional[str]
    libclang_version: Optional[str]
    libclang_library_scope: Optional[str]
    libclang_version_match: Optional[bool]
    type_driven_ast: Optional[bool]
    loc_file_supported: Optional[bool]
    call_reference_supported: Optional[bool]
    member_reference_supported: Optional[bool]
    qual_type_supported: Optional[bool]
    gcc_required: Optional[bool]
    gcc_checked: Optional[bool]
    latest_missing_evidence: Optional[str]
    extractor_worker_mode: Optional[str]
    extractor_worker_count: int
    extractor_worker_max_unmerged: int
    extractor_worker_source_count: int
    extractor_worker_successful_file_count: int
    extractor_worker_skipped_file_count: int
    extractor_worker_map_output_segment_count: int
    extractor_worker_map_output_bytes: int
    extractor_worker_stale_run_gc_count: int
    extractor_worker_timeout_count: int
    extractor_worker_restart_count: int
    extractor_worker_crash_count: int
    relative_map_input_count: int
    relative_map_written_count: int
    relative_map_skipped_exact_count: int
    relative_worker_duplicate_exact_count: int
    relative_worker_duplicate_conflict_count: int
    relative_worker_dedup_tracked_entry_count: int
    relative_worker_dedup_saturated_count: int
    fact_line_passthrough_count: int
    relative_line_passthrough_count: int
    fact_line_passthrough_bytes: int
    relative_line_passthrough_bytes: int
    fact_line_reencoded_count: int
    relative_line_reencoded_count: int
    fact_duplicate_exact_count: int
    fact_duplicate_merge_parse_count: int
    fact_duplicate_conflict_count: int
    relative_duplicate_exact_count: int
    relative_duplicate_conflict_count: int
    relative_merge_input_count: int
    relative_merge_accepted_count: int
    relative_merge_duplicate_exact_count: int
    relative_merge_conflict_count: int
    relative_merge_segment_count: int
    relative_merge_input_bytes: int
    relative_merge_index_bytes: int
    relative_merge_duration_ms: int
    relative_merge_full_parse_count: int
    relative_merge_max_heap_size: int
    relative_merge_fan_in: int
    relative_merge_pass_count: int
    relative_merge_peak_open_segment_count: int
    passthrough_ratio_percent: int
    header_decl_cache_entry_count: int
    header_decl_cache_hit_count: int
    header_decl_cache_miss_count: int
    header_decl_skipped_subtree_count: int
    header_decl_seed_count: int
    source_fallback_count: int
    unresolved_call_count: int
    partial_ast_count: int
    direct_call_pending_count: int
    direct_call_resolved_count: int
    direct_call_external_unresolved_count: int
    direct_call_internal_unresolved_count: int
    direct_call_ambiguous_count: int
    direct_call_linkage_filtered_count: int
    direct_call_missing_caller_count: int
    direct_call_duplicate_relation_count: int
    direct_call_resolver_worker_count: int
    direct_call_pending_shard_count: int
    direct_call_function_index_entry_count: int
    direct_call_resolver_duration_ms: int
    record_owner_count: int
    anonymous_record_count: int
    synthetic_type_fact_count: int
    field_decl_count: int
    field_fact_count: int
    field_decl_without_fact_count: int
    wrapped_member_expr_count: int
    macro_wrapped_member_expr_count: int
    bitwise_member_expr_count: int
    compound_field_access_count: int
    field_access_scan_truncated_count: int
    field_access_resolved_count: int
    field_access_unresolved_count: int
    function_pointer_slot_count: int
    function_pointer_assignment_count: int
    function_pointer_dispatch_count: int
    macro_direct_call_count: int
    unresolved_dispatch_slot_count: int
    unresolved_dispatch_function_count: int
    relative_rollup_group_count: int
    relative_collapsed_instance_count: int
    relative_preview_source_file_count: int
    relative_diversity_bucket_count: int
    response_bytes: int
    response_bytes_limit: int
    response_truncated_count: int
    flat_relative_count: int
    flat_relative_dropped_count: int
    bucket_relative_dropped_count: int
    source_context_line_dropped_count: int
    payload_field_dropped_count: int
    compile_database_configured: bool
    compile_command_hit_count: int
    compile_command_miss_count: int
    compile_command_argument_count: int
    compile_command_stripped_argument_count: int
    compile_command_entry_count: int
    compile_command_indexed_source_count: int
    compile_command_duplicate_source_count: int
    compile_command_ignored_outside_repo_count: int
    latest_toolchain_error: Optional[str]


@dataclass(frozen=True)
class IncrementalViewModel:
    state: str
    base_snapshot_id: Optional[str]
    active_overlay_id: Optional[str]
    dirty_source_count: int
    pending_task_count: int
    stale_source_count: int
    failed_task_count: int
    overlay_fact_count: int
    overlay_relative_count: int
    last_publish_latency_ms: Optional[float]
    latest_error_code: Optional[str]


@dataclass(frozen=True)
class ViewBuildError:
    section: str
    code: str
    message: str


@dataclass(frozen=True)
class ToolsOverviewModel:
    state: str
    generated_at: str
    storage: Optional[StorageViewModel]
    log: Optional[LogViewModel]
    incremental: Optional[IncrementalViewModel]
    errors: List[ViewBuildError]


def build_overview(
    target_repo: Path,
    *,
    include_sections: Optional[List[str]] = None,
    top_n: int = 10,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> ToolsOverviewModel:
    target = Path(target_repo)
    started = _now()
    sections = ["storage", "log", "incremental"] if include_sections is None else list(include_sections)
    request_error = _validate_request(sections, top_n, since, until)
    if request_error is not None:
        overview = ToolsOverviewModel(
            state="error",
            generated_at=_now(),
            storage=None,
            log=None,
            incremental=None,
            errors=[request_error],
        )
        _emit_build_event(target, overview, sections, started)
        return overview
    if not sections:
        overview = ToolsOverviewModel(
            state="empty",
            generated_at=_now(),
            storage=None,
            log=None,
            incremental=None,
            errors=[],
        )
        _emit_build_event(target, overview, sections, started)
        return overview

    storage_model: Optional[StorageViewModel] = None
    log_model: Optional[LogViewModel] = None
    incremental_model: Optional[IncrementalViewModel] = None
    errors: List[ViewBuildError] = []

    if "log" in sections:
        try:
            log_model = _build_log_model(target, top_n, since, until)
        except LogError as exc:
            error = ViewBuildError("log", "log_summary_failed", exc.message)
            errors.append(error)
    if "storage" in sections:
        try:
            storage_model, storage_errors = _build_storage_model(target, top_n, since, until)
            errors.extend(storage_errors)
        except StorageError as exc:
            error = ViewBuildError("storage", "storage_unreadable", exc.message)
            errors.append(error)
    if "incremental" in sections:
        try:
            incremental_model = _build_incremental_model(target, since, until)
        except (IncrementalError, LogError) as exc:
            message = getattr(exc, "message", str(exc))
            error = ViewBuildError("incremental", "incremental_unreadable", message)
            errors.append(error)
    overview = ToolsOverviewModel(
        state=_merge_state(
            [model.state for model in (storage_model, log_model, incremental_model) if model is not None],
            errors,
        ),
        generated_at=_now(),
        storage=storage_model,
        log=log_model,
        incremental=incremental_model,
        errors=errors,
    )
    for error in errors:
        _emit_section_error(target, error)
    _emit_build_event(target, overview, sections, started)
    return overview


def _validate_request(
    sections: List[str],
    top_n: int,
    since: Optional[str],
    until: Optional[str],
) -> Optional[ViewBuildError]:
    if any(section not in ALLOWED_SECTIONS for section in sections):
        return ViewBuildError("*", "invalid_section", "include_sections only allows storage, log, and incremental")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1 or top_n > 50:
        return ViewBuildError("*", "invalid_top_n", "top_n must be between 1 and 50")
    for value in (since, until):
        if value is not None and (not isinstance(value, str) or not value or TIMESTAMP_RE.fullmatch(value) is None):
            return ViewBuildError("*", "invalid_time_window_format", "since and until must use log timestamp format")
    if since is not None and until is not None and since > until:
        return ViewBuildError("*", "invalid_time_window", "since must be before until")
    return None


def _build_storage_model(
    target: Path,
    top_n: int,
    since: Optional[str],
    until: Optional[str],
) -> Tuple[StorageViewModel, List[ViewBuildError]]:
    stats = open_fact_store(target, mode="r").stats()
    errors: List[ViewBuildError] = []
    search_count = 0
    relations_count = 0
    error_count = 0
    latest_error_code: Optional[str] = None
    latest_log_error_code = stats.latest_log_error_code
    log_write_failures = stats.log_write_failures
    try:
        _ensure_log_dir_readable(target)
        storage_summary = open_log(target).summarize(channel="storage", since=since, until=until)
        search_count = storage_summary.events_by_name.get("storage.search", 0)
        relations_count = storage_summary.events_by_name.get("storage.relations", 0)
        error_count = storage_summary.events_by_name.get("storage.error", 0)
        latest_error_code = storage_summary.latest_error_code
    except LogError as exc:
        if latest_log_error_code is None:
            latest_log_error_code = "log_unreadable"
        error = ViewBuildError("storage.log", "log_unreadable", exc.message)
        errors.append(error)

    state = "empty"
    if stats.total_facts > 0:
        state = "ready"
    if log_write_failures > 0 or latest_log_error_code or stats.lock_state == "stale_likely":
        state = "warning"
    if stats.orphan_relative_count > 0:
        state = "error"
    return (
        StorageViewModel(
            state=state,
            snapshot_id=stats.snapshot_id,
            snapshot_format=stats.snapshot_format,
            compression=stats.compression,
            total_facts=stats.total_facts,
            total_relatives=stats.total_relatives,
            total_sources=stats.total_sources,
            fact_kinds=_top_dict(stats.fact_kinds, top_n),
            relation_kinds=_top_dict(stats.relation_kinds, top_n),
            field_read_count=stats.relation_kinds.get("field_read", 0),
            field_write_count=stats.relation_kinds.get("field_write", 0),
            conditional_relative_count=stats.conditional_relative_count,
            orphan_relative_count=stats.orphan_relative_count,
            profiles=_top_dict(stats.profiles, top_n),
            source_files=_top_dict(stats.source_files, top_n),
            snapshot_count=stats.snapshot_count,
            bytes_on_disk=stats.bytes_on_disk,
            bytes_on_disk_total=stats.bytes_on_disk_total,
            uncompressed_bytes=stats.uncompressed_bytes,
            compression_ratio=stats.compression_ratio,
            storage_overhead_ratio=stats.storage_overhead_ratio,
            file_bytes=stats.file_bytes,
            read_index_state=stats.read_index_state,
            read_index_bytes=stats.read_index_bytes,
            read_index_schema_version=stats.read_index_schema_version,
            read_index_codec=stats.read_index_codec,
            lock_state=stats.lock_state,
            search_count=search_count,
            relations_count=relations_count,
            error_count=error_count,
            log_write_failures=log_write_failures,
            latest_error_code=latest_error_code,
            latest_log_error_code=latest_log_error_code,
        ),
        errors,
    )


def _build_log_model(
    target: Path,
    top_n: int,
    since: Optional[str],
    until: Optional[str],
) -> LogViewModel:
    _ensure_log_dir_readable(target)
    summary = open_log(target).summarize(since=since, until=until)
    recent = [_row_from_digest(digest) for digest in summary.recent_events]
    recent.sort(key=lambda row: row.timestamp, reverse=True)
    slow = [_row_from_digest(digest) for digest in summary.slow_events]
    slow.sort(key=lambda row: ((row.duration_ms or 0.0), row.timestamp), reverse=True)
    state = _log_state(summary)
    toolchain = _latest_digest(summary, "extractor.code.toolchain")
    worker_pool = _latest_digest(summary, "extractor.code.worker_pool")
    latest_toolchain_error = _latest_error_for_events(
        summary,
        {"extractor.code.toolchain", "extractor.code.file"},
    )
    return LogViewModel(
        state=state,
        total_events=summary.total_events,
        events_by_channel=dict(summary.events_by_channel),
        events_by_status=dict(summary.events_by_status),
        top_event_names=_top_pairs(summary.events_by_name, top_n),
        recent_events=recent,
        slow_events=slow,
        error_codes=dict(summary.error_codes),
        init_stage_timings=_latest_init_stage_timings(summary),
        duration_ms_total=summary.duration_ms_total,
        malformed_lines=summary.malformed_lines,
        bytes_on_disk=summary.bytes_on_disk,
        latest_event_at=summary.latest_event_at,
        latest_error_code=summary.latest_error_code,
        redaction_summary=summary.redaction_summary,
        dropped_event_count=summary.dropped_event_count,
        toolchain_status=toolchain.status if toolchain is not None else None,
        toolchain_backend=_digest_field(toolchain, "backend"),
        clang_vendor=_digest_field(toolchain, "clang_vendor"),
        clang_version=_digest_field(toolchain, "clang_version"),
        libclang_version=_digest_field(toolchain, "libclang_version"),
        libclang_library_scope=_digest_field(toolchain, "libclang_library_scope"),
        libclang_version_match=_digest_bool(toolchain, "version_match"),
        type_driven_ast=_digest_bool(toolchain, "type_driven_ast"),
        loc_file_supported=_digest_bool(toolchain, "loc_file_supported"),
        call_reference_supported=_digest_bool(toolchain, "call_reference_supported"),
        member_reference_supported=_digest_bool(toolchain, "member_reference_supported"),
        qual_type_supported=_digest_bool(toolchain, "qual_type_supported"),
        gcc_required=_digest_bool(toolchain, "gcc_required"),
        gcc_checked=_digest_bool(toolchain, "gcc_checked"),
        latest_missing_evidence=_digest_field(toolchain, "missing_evidence"),
        extractor_worker_mode=_digest_field(worker_pool, "mode"),
        extractor_worker_count=_digest_count(worker_pool, "worker_count"),
        extractor_worker_max_unmerged=_digest_int_field(worker_pool, "max_unmerged"),
        extractor_worker_source_count=_digest_count(worker_pool, "source_count"),
        extractor_worker_successful_file_count=_digest_count(worker_pool, "successful_file_count"),
        extractor_worker_skipped_file_count=_digest_count(worker_pool, "skipped_file_count"),
        extractor_worker_map_output_segment_count=summary.custom_counts.get("map_output_segment_count", 0),
        extractor_worker_map_output_bytes=summary.custom_counts.get("map_output_bytes", 0),
        extractor_worker_stale_run_gc_count=summary.custom_counts.get("stale_run_gc_count", 0),
        extractor_worker_timeout_count=summary.custom_counts.get("worker_timeout_count", 0),
        extractor_worker_restart_count=summary.custom_counts.get("worker_restart_count", 0),
        extractor_worker_crash_count=summary.custom_counts.get("worker_crash_count", 0),
        relative_map_input_count=summary.custom_counts.get("relative_map_input_count", 0),
        relative_map_written_count=summary.custom_counts.get("relative_map_written_count", 0),
        relative_map_skipped_exact_count=summary.custom_counts.get("relative_map_skipped_exact_count", 0),
        relative_worker_duplicate_exact_count=summary.custom_counts.get("relative_worker_duplicate_exact_count", 0),
        relative_worker_duplicate_conflict_count=summary.custom_counts.get("relative_worker_duplicate_conflict_count", 0),
        relative_worker_dedup_tracked_entry_count=summary.custom_counts.get(
            "relative_worker_dedup_tracked_entry_count",
            0,
        ),
        relative_worker_dedup_saturated_count=summary.custom_counts.get("relative_worker_dedup_saturated_count", 0),
        fact_line_passthrough_count=summary.custom_counts.get("fact_line_passthrough_count", 0),
        relative_line_passthrough_count=summary.custom_counts.get("relative_line_passthrough_count", 0),
        fact_line_passthrough_bytes=summary.custom_counts.get("fact_line_passthrough_bytes", 0),
        relative_line_passthrough_bytes=summary.custom_counts.get("relative_line_passthrough_bytes", 0),
        fact_line_reencoded_count=summary.custom_counts.get("fact_line_reencoded_count", 0),
        relative_line_reencoded_count=summary.custom_counts.get("relative_line_reencoded_count", 0),
        fact_duplicate_exact_count=summary.custom_counts.get("fact_duplicate_exact_count", 0),
        fact_duplicate_merge_parse_count=summary.custom_counts.get("fact_duplicate_merge_parse_count", 0),
        fact_duplicate_conflict_count=summary.custom_counts.get("fact_duplicate_conflict_count", 0),
        relative_duplicate_exact_count=summary.custom_counts.get("relative_duplicate_exact_count", 0),
        relative_duplicate_conflict_count=summary.custom_counts.get("relative_duplicate_conflict_count", 0),
        relative_merge_input_count=summary.custom_counts.get("relative_merge_input_count", 0),
        relative_merge_accepted_count=summary.custom_counts.get("relative_merge_accepted_count", 0),
        relative_merge_duplicate_exact_count=summary.custom_counts.get("relative_merge_duplicate_exact_count", 0),
        relative_merge_conflict_count=summary.custom_counts.get("relative_merge_conflict_count", 0),
        relative_merge_segment_count=summary.custom_counts.get("relative_merge_segment_count", 0),
        relative_merge_input_bytes=summary.custom_counts.get("relative_merge_input_bytes", 0),
        relative_merge_index_bytes=summary.custom_counts.get("relative_merge_index_bytes", 0),
        relative_merge_duration_ms=summary.custom_counts.get("relative_merge_duration_ms", 0),
        relative_merge_full_parse_count=summary.custom_counts.get("relative_merge_full_parse_count", 0),
        relative_merge_max_heap_size=summary.custom_counts.get("relative_merge_max_heap_size", 0),
        relative_merge_fan_in=summary.custom_counts.get("relative_merge_fan_in", 0),
        relative_merge_pass_count=summary.custom_counts.get("relative_merge_pass_count", 0),
        relative_merge_peak_open_segment_count=summary.custom_counts.get("relative_merge_peak_open_segment_count", 0),
        passthrough_ratio_percent=summary.custom_counts.get("passthrough_ratio_percent", 100),
        header_decl_cache_entry_count=summary.custom_counts.get("header_decl_cache_entry_count", 0),
        header_decl_cache_hit_count=summary.custom_counts.get("header_decl_cache_hit_count", 0),
        header_decl_cache_miss_count=summary.custom_counts.get("header_decl_cache_miss_count", 0),
        header_decl_skipped_subtree_count=summary.custom_counts.get("header_decl_skipped_subtree_count", 0),
        header_decl_seed_count=summary.custom_counts.get("header_decl_seed_count", 0),
        source_fallback_count=summary.custom_counts.get("source_fallback_count", 0),
        unresolved_call_count=summary.custom_counts.get("unresolved_call_count", 0),
        partial_ast_count=summary.custom_counts.get("partial_ast_count", 0),
        direct_call_pending_count=summary.custom_counts.get("pending_call_count", 0),
        direct_call_resolved_count=summary.custom_counts.get("resolved_call_count", 0),
        direct_call_external_unresolved_count=summary.custom_counts.get("external_unresolved_count", 0),
        direct_call_internal_unresolved_count=summary.custom_counts.get("internal_unresolved_count", 0),
        direct_call_ambiguous_count=summary.custom_counts.get("ambiguous_call_count", 0),
        direct_call_linkage_filtered_count=summary.custom_counts.get("linkage_filtered_count", 0),
        direct_call_missing_caller_count=summary.custom_counts.get("missing_caller_count", 0),
        direct_call_duplicate_relation_count=summary.custom_counts.get("duplicate_relation_count", 0),
        direct_call_resolver_worker_count=summary.custom_counts.get("resolver_worker_count", 0),
        direct_call_pending_shard_count=summary.custom_counts.get("pending_shard_count", 0),
        direct_call_function_index_entry_count=summary.custom_counts.get("function_index_entry_count", 0),
        direct_call_resolver_duration_ms=summary.custom_counts.get("resolver_duration_ms", 0),
        record_owner_count=summary.custom_counts.get("record_owner_count", 0),
        anonymous_record_count=summary.custom_counts.get("anonymous_record_count", 0),
        synthetic_type_fact_count=summary.custom_counts.get("synthetic_type_fact_count", 0),
        field_decl_count=summary.custom_counts.get("field_decl_count", 0),
        field_fact_count=summary.custom_counts.get("field_fact_count", 0),
        field_decl_without_fact_count=summary.custom_counts.get("field_decl_without_fact_count", 0),
        wrapped_member_expr_count=summary.custom_counts.get("wrapped_member_expr_count", 0),
        macro_wrapped_member_expr_count=summary.custom_counts.get("macro_wrapped_member_expr_count", 0),
        bitwise_member_expr_count=summary.custom_counts.get("bitwise_member_expr_count", 0),
        compound_field_access_count=summary.custom_counts.get("compound_field_access_count", 0),
        field_access_scan_truncated_count=summary.custom_counts.get("field_access_scan_truncated_count", 0),
        field_access_resolved_count=summary.custom_counts.get("field_access_resolved_count", 0),
        field_access_unresolved_count=summary.custom_counts.get("field_access_unresolved_count", 0),
        function_pointer_slot_count=summary.custom_counts.get("function_pointer_slot_count", 0),
        function_pointer_assignment_count=summary.custom_counts.get("function_pointer_assignment_count", 0),
        function_pointer_dispatch_count=summary.custom_counts.get("function_pointer_dispatch_count", 0),
        macro_direct_call_count=summary.custom_counts.get("macro_direct_call_count", 0),
        unresolved_dispatch_slot_count=summary.custom_counts.get("unresolved_dispatch_slot_count", 0),
        unresolved_dispatch_function_count=summary.custom_counts.get("unresolved_dispatch_function_count", 0),
        relative_rollup_group_count=summary.custom_counts.get("relative_rollup_group_count", 0),
        relative_collapsed_instance_count=summary.custom_counts.get("relative_collapsed_instance_count", 0),
        relative_preview_source_file_count=summary.custom_counts.get("relative_preview_source_file_count", 0),
        relative_diversity_bucket_count=summary.custom_counts.get("relative_diversity_bucket_count", 0),
        response_bytes=summary.custom_counts.get("response_bytes", 0),
        response_bytes_limit=summary.custom_counts.get("response_bytes_limit", 0),
        response_truncated_count=summary.custom_counts.get("response_truncated_count", 0),
        flat_relative_count=summary.custom_counts.get("flat_relative_count", 0),
        flat_relative_dropped_count=summary.custom_counts.get("flat_relative_dropped_count", 0),
        bucket_relative_dropped_count=summary.custom_counts.get("bucket_relative_dropped_count", 0),
        source_context_line_dropped_count=summary.custom_counts.get("source_context_line_dropped_count", 0),
        payload_field_dropped_count=summary.custom_counts.get("payload_field_dropped_count", 0),
        compile_database_configured=summary.events_by_name.get("extractor.code.compile_database", 0) > 0,
        compile_command_hit_count=summary.custom_counts.get("compile_command_hit_count", 0),
        compile_command_miss_count=summary.custom_counts.get("compile_command_miss_count", 0),
        compile_command_argument_count=summary.custom_counts.get("compile_command_argument_count", 0),
        compile_command_stripped_argument_count=summary.custom_counts.get("compile_command_stripped_argument_count", 0),
        compile_command_entry_count=summary.custom_counts.get("compile_command_entry_count", 0),
        compile_command_indexed_source_count=summary.custom_counts.get("compile_command_indexed_source_count", 0),
        compile_command_duplicate_source_count=summary.custom_counts.get("compile_command_duplicate_source_count", 0),
        compile_command_ignored_outside_repo_count=summary.custom_counts.get("compile_command_ignored_outside_repo_count", 0),
        latest_toolchain_error=latest_toolchain_error,
    )


def _build_incremental_model(
    target: Path,
    since: Optional[str],
    until: Optional[str],
) -> IncrementalViewModel:
    status = read_incremental_status(target)
    latest = None
    summary = open_log(target).summarize(channel="incremental", since=since, until=until)
    events = open_log(target).read_events(channel="incremental").events
    for event in reversed(events):
        if event.event_name in {"incremental.overlay_published", "incremental.extract_failed", "incremental.dirty_planned"}:
            latest = event
            break
    state = status.state
    base_snapshot_id = status.base_snapshot_id
    active_overlay_id = status.overlay_id
    overlay_fact_count = status.overlay_fact_count
    overlay_relative_count = status.overlay_relative_count
    last_publish_latency_ms = status.last_publish_latency_ms
    latest_error_code = status.latest_error_code or summary.latest_error_code
    dirty_source_count = status.dirty_source_count or summary.custom_counts.get("dirty_source_count", 0)
    pending_task_count = status.pending_task_count
    stale_source_count = status.stale_source_count
    failed_task_count = status.failed_task_count
    if latest is not None:
        if latest.event_name == "incremental.overlay_published":
            state = "overlay"
            base_snapshot_id = _payload_str(latest, "base_snapshot_id") or base_snapshot_id
            active_overlay_id = _payload_str(latest, "overlay_id") or active_overlay_id
            overlay_fact_count = latest.counts.get("overlay_fact_count", overlay_fact_count)
            overlay_relative_count = latest.counts.get("overlay_relative_count", overlay_relative_count)
            latency = latest.payload.get("publish_latency_ms")
            if isinstance(latency, (int, float)):
                last_publish_latency_ms = float(latency)
        elif latest.event_name == "incremental.extract_failed":
            state = "error"
            latest_error_code = latest.error_code or _payload_str(latest, "error_code")
            failed_task_count = max(1, failed_task_count)
        elif latest.event_name == "incremental.dirty_planned" and state in {"disabled", "ready"}:
            state = "stale" if latest.status == "warning" else "pending"
    if state == "disabled" and summary.total_events:
        state = "ready"
    if summary.events_by_status.get("error", 0) > 0:
        state = "error"
    elif state in {"stale", "pending"} or summary.events_by_status.get("warning", 0) > 0:
        state = "warning" if state not in {"stale", "pending"} else state
    return IncrementalViewModel(
        state=state,
        base_snapshot_id=base_snapshot_id,
        active_overlay_id=active_overlay_id,
        dirty_source_count=dirty_source_count,
        pending_task_count=pending_task_count,
        stale_source_count=stale_source_count,
        failed_task_count=failed_task_count,
        overlay_fact_count=overlay_fact_count,
        overlay_relative_count=overlay_relative_count,
        last_publish_latency_ms=last_publish_latency_ms,
        latest_error_code=latest_error_code,
    )


def _row_from_digest(digest: LogEventDigest) -> LogEventRow:
    summary = digest.summary or digest.event_name or "(no summary)"
    detail_parts: List[str] = []
    if digest.subject_id:
        detail_parts.append(f"subject={digest.subject_id}")
    for key, value in digest.counts.items():
        detail_parts.append(f"{key}={value}")
    if digest.error_code:
        detail_parts.append(f"error={digest.error_code}")
    return LogEventRow(
        timestamp=digest.timestamp,
        label=digest.event_name,
        status=digest.status,
        duration_ms=digest.duration_ms,
        summary=summary,
        detail=" ".join(detail_parts),
        fields=list(digest.fields),
        error_code=digest.error_code,
    )


def _latest_digest(summary: LogSummary, event_name: str) -> Optional[LogEventDigest]:
    for digest in reversed(summary.recent_events):
        if digest.event_name == event_name:
            return digest
    return None


def _latest_init_stage_timings(summary: LogSummary) -> List[InitStageView]:
    by_stage: Dict[str, InitStageView] = {}
    latest_collect = None
    for digest in summary.latest_init_stage_events:
        if _digest_field(digest, "stage") == "collect":
            latest_collect = digest.timestamp
    for digest in summary.latest_init_stage_events:
        if digest.event_name != "init.stage":
            continue
        stage = _digest_field(digest, "stage")
        if stage not in INIT_STAGE_ORDER:
            continue
        if latest_collect is not None and digest.timestamp < latest_collect:
            continue
        by_stage[stage] = InitStageView(
            stage=stage,
            status=digest.status,
            duration_ms=float(digest.duration_ms or 0.0),
            counts=dict(digest.counts),
        )
    return [by_stage[stage] for stage in INIT_STAGE_ORDER if stage in by_stage]


def _latest_error_for_events(summary: LogSummary, event_names: set[str]) -> Optional[str]:
    for digest in reversed(summary.recent_events):
        if digest.event_name in event_names and digest.error_code:
            return digest.error_code
    return None


def _digest_field(digest: Optional[LogEventDigest], key: str) -> Optional[str]:
    if digest is None:
        return None
    for name, value in digest.fields:
        if name == key:
            return value
    return None


def _digest_bool(digest: Optional[LogEventDigest], key: str) -> Optional[bool]:
    value = _digest_field(digest, key)
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def _digest_count(digest: Optional[LogEventDigest], key: str) -> int:
    if digest is None:
        return 0
    return digest.counts.get(key, 0)


def _digest_int_field(digest: Optional[LogEventDigest], key: str) -> int:
    value = _digest_field(digest, key)
    if value is None:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _payload_str(event: LogEvent, key: str) -> Optional[str]:
    value = event.payload.get(key)
    return value if isinstance(value, str) else None


def _log_state(summary: LogSummary) -> str:
    if summary.total_events == 0:
        return "empty"
    if summary.events_by_status.get("error", 0) > 0:
        return "error"
    if (
        summary.malformed_lines > 0
        or summary.dropped_event_count > 0
        or summary.events_by_status.get("warning", 0) > 0
        or summary.custom_counts.get("field_decl_without_fact_count", 0) > 0
        or summary.custom_counts.get("field_access_unresolved_count", 0) > 0
        or summary.custom_counts.get("field_access_scan_truncated_count", 0) > 0
        or summary.custom_counts.get("unresolved_dispatch_slot_count", 0) > 0
        or summary.custom_counts.get("unresolved_dispatch_function_count", 0) > 0
        or summary.custom_counts.get("partial_ast_count", 0) > 0
        or summary.custom_counts.get("internal_unresolved_count", 0) > 0
        or summary.custom_counts.get("ambiguous_call_count", 0) > 0
        or summary.custom_counts.get("linkage_filtered_count", 0) > 0
        or (
            summary.events_by_name.get("extractor.code.compile_database", 0) > 0
            and summary.custom_counts.get("compile_command_miss_count", 0) > 0
        )
    ):
        return "warning"
    return "ready"


def _merge_state(states: List[str], errors: List[ViewBuildError]) -> str:
    fatal_errors = [error for error in errors if error.section != "storage.log"]
    if fatal_errors or "error" in states:
        return "error"
    if errors or "warning" in states:
        return "warning"
    if not states or all(state in {"empty", "disabled"} for state in states):
        return "empty"
    return "ready"


def _top_dict(values: Dict[str, int], top_n: int) -> Dict[str, int]:
    return dict(_top_pairs(values, top_n))


def _top_pairs(values: Dict[str, int], top_n: int) -> List[Tuple[str, int]]:
    return sorted(values.items(), key=lambda item: (-item[1], item[0]))[:top_n]


def _ensure_log_dir_readable(target: Path) -> None:
    log_dir = target / ".cipher" / "log"
    if log_dir.exists() and not log_dir.is_dir():
        raise LogError("log_read_failed", "log path is not a directory", path=log_dir)


def _emit_section_error(target: Path, error: ViewBuildError) -> None:
    try:
        open_log(target).write_event(
            LogEvent(
                event_name="views.section_error",
                channel="views",
                status="error",
                error_code=error.code,
                payload={"section": error.section, "error_code": error.code},
                summary=error.message,
            )
        )
    except LogError:
        pass


def _emit_build_event(
    target: Path,
    overview: ToolsOverviewModel,
    sections: List[str],
    started: str,
) -> None:
    try:
        open_log(target).write_event(
            LogEvent(
                event_name="views.build",
                channel="views",
                status="ok" if overview.state != "error" else "error",
                error_code="overview_error" if overview.state == "error" else None,
                duration_ms=_elapsed_ms(started),
                counts={"section_count": len(sections), "error_count": len(overview.errors)},
                payload={"state": overview.state, "section_count": len(sections), "error_count": len(overview.errors)},
            )
        )
    except LogError:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _elapsed_ms(started: str) -> float:
    try:
        start = datetime.strptime(started, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - start).total_seconds() * 1000)


__all__ = [
    "InitStageView",
    "LogEventRow",
    "LogViewModel",
    "IncrementalViewModel",
    "StorageViewModel",
    "ToolsOverviewModel",
    "ViewBuildError",
    "ViewBuildRequest",
    "build_overview",
]

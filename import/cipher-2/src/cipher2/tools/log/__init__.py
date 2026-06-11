"""Append-only JSONL logging for target cipher repositories."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cipher2.common import JSONValue

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms.
    fcntl = None


SAFE_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
DEFAULT_REDACTION_RULES = [
    re.compile(pattern)
    for pattern in (
        r"(?i)password",
        r"(?i)secret",
        r"(?i)token",
        r"(?i)api[_-]?key",
        r"(?i)authorization",
        r"(?i)cookie",
    )
]
ALLOWED_STATUSES = {"ok", "error", "warning"}
MAX_CHANNELS = 32
MAX_LINE_BYTES = 64 * 1024
DEFAULT_MAX_STRING = 512
DEFAULT_MAX_COLLECTION = 50
DEFAULT_MAX_DEPTH = 5
LOG_SCHEMA_VERSION = 2
SUPPORTED_LOG_SCHEMA_VERSIONS = {1, LOG_SCHEMA_VERSION}
REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
DIGEST_COUNT_FIELD_LIMIT = 8
DIGEST_FIELD_LIMIT = 16
DIGEST_PRIORITY_COUNT_FIELDS = (
    "partial_ast_count",
    "warning_count",
    "field_decl_without_fact_count",
    "field_access_unresolved_count",
    "field_access_scan_truncated_count",
    "unresolved_dispatch_slot_count",
    "unresolved_dispatch_function_count",
    "relative_rollup_group_count",
    "relative_collapsed_instance_count",
    "relative_preview_source_file_count",
    "relative_diversity_bucket_count",
    "response_bytes",
    "response_bytes_limit",
    "response_truncated_count",
    "flat_relative_count",
    "flat_relative_dropped_count",
    "bucket_relative_dropped_count",
    "source_context_line_dropped_count",
    "payload_field_dropped_count",
    "matched_endpoint_count",
    "too_broad_count",
    "pending_call_count",
    "resolved_call_count",
    "internal_unresolved_count",
    "ambiguous_call_count",
    "linkage_filtered_count",
    "resolver_worker_count",
    "pending_shard_count",
    "compile_command_miss_count",
    "bytes_written",
    "uncompressed_bytes",
    "compressed_data_bytes",
    "read_index_bytes",
    "read_index_build_ms",
    "read_index_open_ms",
    "compression_ratio_percent",
    "storage_overhead_ratio_percent",
    "fact_count",
    "relative_count",
    "source_count",
    "worker_count",
    "successful_file_count",
    "skipped_file_count",
    "header_decl_cache_entry_count",
    "map_output_segment_count",
    "relative_map_input_count",
    "relative_map_written_count",
    "relative_map_skipped_exact_count",
    "relative_worker_duplicate_exact_count",
    "relative_worker_duplicate_conflict_count",
    "relative_worker_dedup_tracked_entry_count",
    "relative_worker_dedup_saturated_count",
    "worker_timeout_count",
    "worker_restart_count",
    "worker_crash_count",
    "fact_line_passthrough_count",
    "relative_line_passthrough_count",
    "fact_line_reencoded_count",
    "relative_line_reencoded_count",
    "relative_merge_input_count",
    "relative_merge_accepted_count",
    "relative_merge_duplicate_exact_count",
    "relative_merge_conflict_count",
    "relative_merge_segment_count",
    "relative_merge_fan_in",
    "relative_merge_pass_count",
    "relative_merge_peak_open_segment_count",
    "relative_merge_full_parse_count",
    "relative_merge_input_bytes",
    "relative_merge_index_bytes",
    "relative_merge_duration_ms",
    "relative_merge_max_heap_size",
    "fact_duplicate_merge_parse_count",
    "passthrough_ratio_percent",
    "header_decl_cache_hit_count",
    "header_decl_skipped_subtree_count",
    "header_decl_cache_miss_count",
    "header_decl_seed_count",
)
ALWAYS_DIGEST_ZERO_COUNT_FIELDS = {"relative_merge_full_parse_count"}
PAYLOAD_FIELD_ORDER = [
    "operation",
    "outcome",
    "stage",
    "stage_duration_ms",
    "mode",
    "max_unmerged",
    "snapshot_id",
    "snapshot_format",
    "compression",
    "read_index_format",
    "read_index_codec",
    "index_backend",
    "query_kind",
    "query_preview",
    "relation_predicate",
    "matched_count",
    "matched_endpoint_count",
    "returned_count",
    "limit",
    "term_count",
    "anchor_candidate_count",
    "too_broad_count",
    "filter_count",
    "direction",
    "relation_kind",
    "has_compile_database",
    "compile_database_configured",
    "compile_database_candidate_count",
    "compile_database_scope",
    "clang_executable_scope",
    "libclang_library_scope",
    "gcc_executable_scope",
    "clang_arg_count",
    "extractor_worker_count",
    "backend",
    "clang_vendor",
    "clang_version",
    "libclang_version",
    "version_match",
    "ast_json_supported",
    "type_driven_ast",
    "loc_file_supported",
    "call_reference_supported",
    "member_reference_supported",
    "qual_type_supported",
    "missing_evidence",
    "ast_root_kind",
    "gcc_required",
    "gcc_checked",
    "source_kind",
    "parse_duration_ms",
    "traverse_duration_ms",
    "diagnostic_kind",
    "incremental_enabled",
    "incremental_worker_count",
    "incremental_poll_interval_ms",
    "legacy_section_count",
    "config_exists",
    "error_code",
    "fact_count",
    "relative_count",
    "relative_map_input_count",
    "relative_map_written_count",
    "relative_map_skipped_exact_count",
    "field_read_count",
    "field_write_count",
    "typed_member_expr_count",
    "typed_call_expr_count",
    "source_from_loc_file_count",
    "source_fallback_count",
    "unresolved_call_count",
    "partial_ast_count",
    "pending_call_count",
    "resolved_call_count",
    "external_unresolved_count",
    "internal_unresolved_count",
    "ambiguous_call_count",
    "linkage_filtered_count",
    "missing_caller_count",
    "duplicate_relation_count",
    "field_owner_count",
    "record_owner_count",
    "anonymous_record_count",
    "synthetic_type_fact_count",
    "field_decl_count",
    "field_fact_count",
    "field_decl_without_fact_count",
    "wrapped_member_expr_count",
    "macro_wrapped_member_expr_count",
    "bitwise_member_expr_count",
    "compound_field_access_count",
    "field_access_scan_truncated_count",
    "field_access_resolved_count",
    "field_access_unresolved_count",
    "function_pointer_slot_count",
    "function_pointer_assignment_count",
    "function_pointer_dispatch_count",
    "macro_direct_call_count",
    "unresolved_dispatch_slot_count",
    "unresolved_dispatch_function_count",
    "header_decl_cache_entry_count",
    "header_decl_cache_hit_count",
    "header_decl_cache_miss_count",
    "header_decl_skipped_subtree_count",
    "header_decl_seed_count",
    "compile_command_hit_count",
    "compile_command_miss_count",
    "compile_command_argument_count",
    "compile_command_stripped_argument_count",
    "compile_command_entry_count",
    "compile_command_indexed_source_count",
    "compile_command_duplicate_source_count",
    "compile_command_ignored_outside_repo_count",
    "source_count",
    "worker_count",
    "successful_file_count",
    "skipped_file_count",
    "map_output_segment_count",
    "map_output_bytes",
    "stale_run_gc_count",
    "resolver_worker_count",
    "pending_shard_count",
    "function_index_entry_count",
    "resolver_duration_ms",
    "max_unmerged",
    "conditional_relative_count",
    "relative_rollup_group_count",
    "relative_collapsed_instance_count",
    "relative_preview_source_file_count",
    "relative_diversity_bucket_count",
    "bytes_written",
    "uncompressed_bytes",
    "compressed_data_bytes",
    "read_index_bytes",
    "read_index_build_ms",
    "read_index_open_ms",
    "compression_ratio_percent",
    "storage_overhead_ratio_percent",
    "facts_raw_bytes",
    "facts_compressed_bytes",
    "relatives_raw_bytes",
    "relatives_compressed_bytes",
    "source_inventory_raw_bytes",
    "source_inventory_compressed_bytes",
    "latest_log_error_code",
    "method",
    "tool_name",
    "request_kind",
    "budget",
    "response_truncated",
    "command_name",
    "exit_code",
    "json_output",
    "source_root_count",
    "profile",
    "mcp_config_path",
    "mcp_config_action",
    "server_name",
    "view_state",
    "overview_state",
    "base_snapshot_id",
    "overlay_id",
    "stale_source_count",
    "pending_task_count",
    "dirty_reason",
    "overlay_reason",
    "publish_latency_ms",
]


class LogError(Exception):
    """Structured log module error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: Optional[Path] = None,
        details: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = str(path) if path is not None else None
        self.details = dict(details or {})


@dataclass(frozen=True)
class LogWriteResult:
    path: Path
    bytes_written: int


@dataclass(frozen=True)
class LogReadIssue:
    line_number: int
    error_code: str
    message: str
    path: Optional[Path] = None

    @property
    def code(self) -> str:
        return self.error_code


@dataclass(frozen=True)
class LogReadResult:
    events: List["LogEvent"]
    issues: List[LogReadIssue]


@dataclass(frozen=True)
class LogEventDigest:
    timestamp: str
    event_name: str
    status: str
    duration_ms: Optional[float]
    channel: str
    subject_id: Optional[str]
    summary: Optional[str]
    error_code: Optional[str]
    counts: Dict[str, int]
    fields: List[Tuple[str, str]]


@dataclass(frozen=True)
class LogSummary:
    total_events: int
    events_by_channel: Dict[str, int]
    events_by_name: Dict[str, int]
    events_by_status: Dict[str, int]
    error_codes: Dict[str, int]
    duration_ms_total: float
    custom_counts: Dict[str, int]
    malformed_lines: int
    bytes_on_disk: int
    latest_event_at: Optional[str]
    latest_error_code: Optional[str]
    dropped_field_count: int
    truncated_field_count: int
    dropped_event_count: int
    recent_events: List[LogEventDigest]
    slow_events: List[LogEventDigest]
    latest_init_stage_events: List[LogEventDigest] = field(default_factory=list)
    query: Optional[Dict[str, JSONValue]] = None

    @property
    def redaction_summary(self) -> Dict[str, int]:
        return {
            "dropped_field_count": self.dropped_field_count,
            "truncated_field_count": self.truncated_field_count,
        }


@dataclass
class LogEvent:
    event_name: str
    channel: str
    schema_version: int = LOG_SCHEMA_VERSION
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    status: str = "ok"
    duration_ms: Optional[float] = None
    correlation_id: Optional[str] = None
    subject_id: Optional[str] = None
    summary: Optional[str] = None
    counts: Dict[str, int] = field(default_factory=dict)
    error_code: Optional[str] = None
    payload: Dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._validate()

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_name": self.event_name,
            "timestamp": self.timestamp,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "channel": self.channel,
            "correlation_id": self.correlation_id,
            "subject_id": self.subject_id,
            "summary": self.summary,
            "counts": dict(self.counts),
            "error_code": self.error_code,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_json(cls, row: Dict[str, Any]) -> "LogEvent":
        if not isinstance(row, dict):
            raise LogError("invalid_event", "log row must be a JSON object")
        try:
            return cls(
                schema_version=row.get("schema_version", 1),
                event_name=row["event_name"],
                timestamp=row["timestamp"],
                status=row.get("status", "ok"),
                duration_ms=row.get("duration_ms"),
                channel=row["channel"],
                correlation_id=row.get("correlation_id"),
                subject_id=row.get("subject_id"),
                summary=row.get("summary"),
                counts=row.get("counts", {}),
                error_code=row.get("error_code"),
                payload=row.get("payload", {}),
            )
        except KeyError as exc:
            raise LogError("invalid_event", "log row is missing a required field", details={"field": str(exc)}) from exc

    def _validate(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version not in SUPPORTED_LOG_SCHEMA_VERSIONS
        ):
            raise LogError("invalid_event", "unsupported log schema version")
        if not isinstance(self.event_name, str) or EVENT_NAME_RE.fullmatch(self.event_name) is None:
            raise LogError("invalid_event", "event_name must use lowercase dot-separated segments")
        safe_channel_name(self.channel)
        if self.status not in ALLOWED_STATUSES:
            raise LogError("invalid_event", "status must be ok, error, or warning")
        if self.status == "error" and not self.error_code:
            raise LogError("invalid_event", "error status requires error_code")
        _validate_timestamp(self.timestamp)
        if self.duration_ms is not None and not _is_number(self.duration_ms):
            raise LogError("invalid_event", "duration_ms must be a finite number")
        if self.correlation_id is not None and not isinstance(self.correlation_id, str):
            raise LogError("invalid_event", "correlation_id must be a string")
        if self.subject_id is not None and not isinstance(self.subject_id, str):
            raise LogError("invalid_event", "subject_id must be a string")
        if self.summary is not None and not isinstance(self.summary, str):
            raise LogError("invalid_event", "summary must be a string")
        _validate_counts(self.counts)
        if self.error_code is not None and not isinstance(self.error_code, str):
            raise LogError("invalid_event", "error_code must be a string")
        if not isinstance(self.payload, dict):
            raise LogError("invalid_event", "payload must be a JSON object")
        _ensure_json_value(self.payload)


class JsonlLog:
    def __init__(self, target_repo: Path) -> None:
        self.target_repo = Path(target_repo)
        self.locks_by_path: Dict[Path, threading.Lock] = {}
        self.dropped_event_count = 0
        self.stderr_reported = False

    def write_event(self, event: LogEvent, *, observe: bool = False) -> LogWriteResult:
        result = self._append_event(event)
        if observe:
            self._observe_success("log.write", event.channel, {"bytes_written": result.bytes_written})
        return result

    def observe_batch(self, channel: str, counts: Dict[str, int]) -> LogWriteResult:
        safe_channel_name(channel)
        _validate_counts(counts)
        return self._append_event(
            LogEvent(
                event_name=f"{channel}.batch_summary",
                channel=channel,
                counts=dict(counts),
            )
        )

    def read_events(
        self,
        *,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> LogReadResult:
        self._validate_read_args(channel, limit, since, until)
        if limit == 0:
            self._observe_success(
                "log.read",
                channel or "all",
                {"event_count": 0, "malformed_lines": 0},
            )
            return LogReadResult(events=[], issues=[])
        events: List[LogEvent] = []
        issues: List[LogReadIssue] = []
        for path in self._selected_paths(channel):
            for event, issue in self._iter_path(path, since=since, until=until):
                if issue is not None:
                    issues.append(issue)
                    continue
                if event is None:
                    continue
                events.append(event)
                if limit is not None and len(events) >= limit:
                    break
            if limit is not None and len(events) >= limit:
                break
        self._observe_success(
            "log.read",
            channel or "all",
            {"event_count": len(events), "malformed_lines": len(issues)},
        )
        return LogReadResult(events=events, issues=issues)

    def summarize(
        self,
        *,
        channel: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> LogSummary:
        self._validate_read_args(channel, None, since, until)
        total_events = 0
        events_by_channel: Counter[str] = Counter()
        events_by_name: Counter[str] = Counter()
        events_by_status: Counter[str] = Counter()
        error_codes: Counter[str] = Counter()
        custom_counts: Counter[str] = Counter()
        duration_ms_total = 0.0
        malformed_lines = 0
        dropped_field_count = 0
        truncated_field_count = 0
        latest_event_at: Optional[str] = None
        latest_error_at: Optional[str] = None
        latest_error_code: Optional[str] = None
        sequence = 0
        recent: List[Tuple[str, int, Dict[str, Any]]] = []
        slow: List[Tuple[float, str, int, Dict[str, Any]]] = []
        latest_init_stage_events: Dict[str, Tuple[str, int, Dict[str, Any]]] = {}
        paths = self._selected_paths(channel)
        bytes_on_disk = sum(path.stat().st_size for path in paths if path.exists())

        for path in paths:
            for event, issue in self._iter_summary_path(path, since=since, until=until):
                if issue is not None:
                    malformed_lines += 1
                    continue
                if event is None:
                    continue
                total_events += 1
                event_channel = event["channel"]
                event_name = event["event_name"]
                event_status = event["status"]
                event_error_code = event["error_code"]
                event_duration_ms = event["duration_ms"]
                event_timestamp = event["timestamp"]
                event_counts = event["counts"]
                event_payload = event["payload"]
                events_by_channel[event_channel] += 1
                events_by_name[event_name] += 1
                events_by_status[event_status] += 1
                if event_error_code:
                    error_codes[event_error_code] += 1
                    if latest_error_at is None or event_timestamp >= latest_error_at:
                        latest_error_at = event_timestamp
                        latest_error_code = event_error_code
                if event_duration_ms is not None:
                    duration_ms_total += float(event_duration_ms)
                for key, value in event_counts.items():
                    custom_counts[key] += value
                dropped_field_count += _count_value(event_payload, REDACTED)
                truncated_field_count += _count_truncated(event_payload)
                if latest_event_at is None or event_timestamp > latest_event_at:
                    latest_event_at = event_timestamp
                if event_name == "init.stage":
                    stage = event_payload.get("stage")
                    if isinstance(stage, str):
                        existing = latest_init_stage_events.get(stage)
                        if existing is None or (event_timestamp, sequence) >= (existing[0], existing[1]):
                            latest_init_stage_events[stage] = (event_timestamp, sequence, event)
                _keep_recent(recent, (event_timestamp, sequence, event), 20)
                if event_duration_ms is not None:
                    _keep_slow(slow, (float(event_duration_ms), event_timestamp, sequence, event), 20)
                sequence += 1

        summary = LogSummary(
            total_events=total_events,
            events_by_channel=dict(events_by_channel),
            events_by_name=dict(events_by_name),
            events_by_status=dict(events_by_status),
            error_codes=dict(error_codes),
            duration_ms_total=duration_ms_total,
            custom_counts=dict(custom_counts),
            malformed_lines=malformed_lines,
            bytes_on_disk=bytes_on_disk,
            latest_event_at=latest_event_at,
            latest_error_code=latest_error_code,
            dropped_field_count=dropped_field_count,
            truncated_field_count=truncated_field_count,
            dropped_event_count=self.dropped_event_count,
            recent_events=[
                _make_digest_from_mapping(item[2])
                for item in sorted(recent, key=lambda item: (item[0], item[1]))
            ],
            slow_events=[
                _make_digest_from_mapping(item[3])
                for item in sorted(
                    slow,
                    key=lambda item: (item[0], item[1], -item[2]),
                    reverse=True,
                )
            ],
            latest_init_stage_events=[
                _make_digest_from_mapping(item[2])
                for item in sorted(latest_init_stage_events.values(), key=lambda item: (item[0], item[1]))
            ],
            query={"channel": channel, "since": since, "until": until},
        )
        self._observe_success(
            "log.summary",
            channel or "all",
            {
                "event_count": total_events,
                "malformed_lines": malformed_lines,
                "dropped_field_count": dropped_field_count,
                "truncated_field_count": truncated_field_count,
                "dropped_event_count": self.dropped_event_count,
            },
        )
        return summary

    def _append_event(self, event: LogEvent) -> LogWriteResult:
        path = self._channel_path(event.channel)
        row = self._event_row(event)
        line = _dumps_line(row)
        if len(line) > MAX_LINE_BYTES:
            row["summary"] = _truncate_string(row.get("summary"), DEFAULT_MAX_STRING)
            row["payload"] = {"_truncated": TRUNCATED}
            line = _dumps_line(row)
        lock = self._lock_for_path(path)
        try:
            if fcntl is None:
                raise LogError("unsupported_platform_lock", "POSIX fcntl.flock is required", path=path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with lock:
                with path.open("a", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        handle.write(line)
                        handle.flush()
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except LogError as exc:
            self._record_write_failure(path, exc)
            raise
        except OSError as exc:
            error = LogError("log_write_failed", "failed to append log event", path=path, details={"reason": str(exc)})
            self._record_write_failure(path, error)
            raise error from exc
        return LogWriteResult(path=path, bytes_written=len(line.encode("utf-8")))

    def _event_row(self, event: LogEvent) -> Dict[str, Any]:
        row = event.to_json()
        row["payload"] = truncate_value(redact_value(row["payload"]))
        row["summary"] = _truncate_string(row["summary"], DEFAULT_MAX_STRING)
        return row

    def _lock_for_path(self, path: Path) -> threading.Lock:
        if path not in self.locks_by_path:
            if len(self.locks_by_path) >= MAX_CHANNELS:
                raise LogError("too_many_channels", "too many log channels", path=path)
            self.locks_by_path[path] = threading.Lock()
        return self.locks_by_path[path]

    def _channel_path(self, channel: str) -> Path:
        safe = safe_channel_name(channel)
        base = self.target_repo / ".cipher" / "log"
        path = base / f"{safe}.jsonl"
        base_resolved = base.resolve(strict=False)
        path_resolved = path.resolve(strict=False)
        if not _is_relative_to(path_resolved, base_resolved):
            raise LogError("path_escape", "log path escapes .cipher/log", path=path)
        return path

    def _selected_paths(self, channel: Optional[str]) -> List[Path]:
        if channel is not None:
            return [self._channel_path(channel)]
        base = (self.target_repo / ".cipher" / "log").resolve(strict=False)
        if not base.exists():
            return []
        return sorted(path for path in base.glob("*.jsonl") if path.is_file() and _is_relative_to(path.resolve(strict=False), base))

    def _iter_path(
        self,
        path: Path,
        *,
        since: Optional[str],
        until: Optional[str],
    ) -> Iterable[Tuple[Optional[LogEvent], Optional[LogReadIssue]]]:
        if not path.exists():
            return
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end_position = handle.tell()
                handle.seek(0)
                line_number = 0
                while handle.tell() < end_position:
                    raw = handle.readline()
                    if raw == b"":
                        break
                    line_number += 1
                    if raw in (b"\n", b"\r\n"):
                        continue
                    if len(raw) > MAX_LINE_BYTES:
                        yield None, LogReadIssue(line_number, "oversized_line", "log line exceeds 64KB", path)
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        yield None, LogReadIssue(line_number, "malformed_json", str(exc), path)
                        continue
                    try:
                        event = LogEvent.from_json(row)
                    except LogError as exc:
                        yield None, LogReadIssue(line_number, "invalid_schema", exc.message, path)
                        continue
                    if since is not None and event.timestamp < since:
                        continue
                    if until is not None and event.timestamp > until:
                        continue
                    yield event, None
        except OSError as exc:
            yield None, LogReadIssue(0, "log_read_failed", str(exc), path)

    def _iter_summary_path(
        self,
        path: Path,
        *,
        since: Optional[str],
        until: Optional[str],
    ) -> Iterable[Tuple[Optional[Dict[str, Any]], Optional[LogReadIssue]]]:
        if not path.exists():
            return
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end_position = handle.tell()
                handle.seek(0)
                line_number = 0
                while handle.tell() < end_position:
                    raw = handle.readline()
                    if raw == b"":
                        break
                    line_number += 1
                    if raw in (b"\n", b"\r\n"):
                        continue
                    if len(raw) > MAX_LINE_BYTES:
                        yield None, LogReadIssue(line_number, "oversized_line", "log line exceeds 64KB", path)
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        yield None, LogReadIssue(line_number, "malformed_json", str(exc), path)
                        continue
                    try:
                        event = _summary_event_from_json(row)
                    except LogError as exc:
                        yield None, LogReadIssue(line_number, "invalid_schema", exc.message, path)
                        continue
                    if since is not None and event["timestamp"] < since:
                        continue
                    if until is not None and event["timestamp"] > until:
                        continue
                    yield event, None
        except OSError as exc:
            yield None, LogReadIssue(0, "log_read_failed", str(exc), path)

    def _validate_read_args(
        self,
        channel: Optional[str],
        limit: Optional[int],
        since: Optional[str],
        until: Optional[str],
    ) -> None:
        if channel is not None:
            safe_channel_name(channel)
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
            raise LogError("invalid_limit", "limit must be a non-negative integer")
        if since is not None:
            _validate_timestamp(since)
        if until is not None:
            _validate_timestamp(until)
        if since is not None and until is not None and since > until:
            raise LogError("invalid_time_window", "since must be before until")

    def _observe_success(self, event_name: str, observed_channel: str, counts: Dict[str, int]) -> None:
        try:
            self._append_event(
                LogEvent(
                    event_name=event_name,
                    channel="log",
                    counts=counts,
                    payload={"channel": observed_channel},
                )
            )
        except LogError:
            pass

    def _record_write_failure(self, path: Path, error: LogError) -> None:
        self.dropped_event_count += 1
        if not self.stderr_reported:
            self.stderr_reported = True
            print(f"cipher2 log write failed: path={path} error_code={error.code}", file=sys.stderr)


def open_log(target_repo: Path) -> JsonlLog:
    return JsonlLog(Path(target_repo))


def safe_channel_name(channel: str) -> str:
    if not isinstance(channel, str) or SAFE_CHANNEL_RE.fullmatch(channel) is None:
        raise LogError("invalid_channel", "invalid log channel name")
    return channel


def redact_value(value: JSONValue, rules: Optional[List[re.Pattern[str]]] = None) -> JSONValue:
    compiled_rules = rules if rules is not None else DEFAULT_REDACTION_RULES
    if isinstance(value, dict):
        redacted: Dict[str, JSONValue] = {}
        for key, item in value.items():
            if any(rule.search(str(key)) for rule in compiled_rules):
                redacted[str(key)] = REDACTED
            else:
                redacted[str(key)] = redact_value(item, compiled_rules)
        return redacted
    if isinstance(value, list):
        return [redact_value(item, compiled_rules) for item in value]
    return value


def truncate_value(
    value: JSONValue,
    max_string: int = DEFAULT_MAX_STRING,
    max_collection: int = DEFAULT_MAX_COLLECTION,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> JSONValue:
    return _truncate_value(value, max_string=max_string, max_collection=max_collection, max_depth=max_depth, depth=0)


def _truncate_value(
    value: JSONValue,
    *,
    max_string: int,
    max_collection: int,
    max_depth: int,
    depth: int,
) -> JSONValue:
    if depth >= max_depth and isinstance(value, (dict, list)):
        return TRUNCATED
    if isinstance(value, str):
        return _truncate_string(value, max_string)
    if isinstance(value, list):
        items = [
            _truncate_value(
                item,
                max_string=max_string,
                max_collection=max_collection,
                max_depth=max_depth,
                depth=depth + 1,
            )
            for item in value[:max_collection]
        ]
        if len(value) > max_collection and items:
            items[-1] = TRUNCATED
        return items
    if isinstance(value, dict):
        limited_items = list(value.items())[:max_collection]
        result: Dict[str, JSONValue] = {}
        for key, item in limited_items:
            result[str(key)] = _truncate_value(
                item,
                max_string=max_string,
                max_collection=max_collection,
                max_depth=max_depth,
                depth=depth + 1,
            )
        if len(value) > max_collection:
            if len(result) >= max_collection:
                result.pop(next(reversed(result)))
            result["_truncated"] = TRUNCATED
        return result
    return value


def _truncate_string(value: Any, max_string: int) -> Any:
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string] + "..." + TRUNCATED
    return value


def _validate_counts(counts: Dict[str, int]) -> None:
    if not isinstance(counts, dict):
        raise LogError("invalid_event", "counts must be a dict")
    for key, value in counts.items():
        if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
            raise LogError("invalid_event", "counts values must be integers")


def _validate_timestamp(timestamp: str) -> None:
    if not isinstance(timestamp, str) or TIMESTAMP_RE.fullmatch(timestamp) is None:
        raise LogError("invalid_event", "timestamp must use UTC microsecond format")
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise LogError("invalid_event", "timestamp must be a valid UTC time") from exc


def _ensure_json_value(value: Any) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LogError("invalid_event", "value must be JSON serializable") from exc


def _summary_event_from_json(row: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(row, dict):
        raise LogError("invalid_event", "log row must be a JSON object")
    schema_version = row.get("schema_version", 1)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_LOG_SCHEMA_VERSIONS
    ):
        raise LogError("invalid_event", "unsupported log schema version")
    event_name = row.get("event_name")
    if not isinstance(event_name, str) or EVENT_NAME_RE.fullmatch(event_name) is None:
        raise LogError("invalid_event", "event_name must use lowercase dot-separated segments")
    channel = row.get("channel")
    safe_channel_name(channel)
    timestamp = row.get("timestamp")
    if not isinstance(timestamp, str) or TIMESTAMP_RE.fullmatch(timestamp) is None:
        raise LogError("invalid_event", "timestamp must use UTC microsecond format")
    status = row.get("status", "ok")
    if status not in ALLOWED_STATUSES:
        raise LogError("invalid_event", "status must be ok, error, or warning")
    duration_ms = row.get("duration_ms")
    if duration_ms is not None and not _is_number(duration_ms):
        raise LogError("invalid_event", "duration_ms must be a finite number")
    correlation_id = row.get("correlation_id")
    if correlation_id is not None and not isinstance(correlation_id, str):
        raise LogError("invalid_event", "correlation_id must be a string")
    subject_id = row.get("subject_id")
    if subject_id is not None and not isinstance(subject_id, str):
        raise LogError("invalid_event", "subject_id must be a string")
    summary = row.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise LogError("invalid_event", "summary must be a string")
    counts = row.get("counts", {})
    _validate_counts(counts)
    error_code = row.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        raise LogError("invalid_event", "error_code must be a string")
    if status == "error" and not error_code:
        raise LogError("invalid_event", "error status requires error_code")
    payload = row.get("payload", {})
    if not isinstance(payload, dict):
        raise LogError("invalid_event", "payload must be a JSON object")
    return {
        "event_name": event_name,
        "timestamp": timestamp,
        "status": status,
        "duration_ms": duration_ms,
        "channel": channel,
        "correlation_id": correlation_id,
        "subject_id": subject_id,
        "summary": summary,
        "counts": counts,
        "error_code": error_code,
        "payload": payload,
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _dumps_line(row: Dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _make_digest(event: LogEvent) -> LogEventDigest:
    return _make_digest_from_values(
        timestamp=event.timestamp,
        event_name=event.event_name,
        status=event.status,
        duration_ms=event.duration_ms,
        channel=event.channel,
        subject_id=event.subject_id,
        summary=event.summary,
        error_code=event.error_code,
        counts=event.counts,
        payload=event.payload,
        correlation_id=event.correlation_id,
    )


def _make_digest_from_mapping(event: Dict[str, Any]) -> LogEventDigest:
    return _make_digest_from_values(
        timestamp=event["timestamp"],
        event_name=event["event_name"],
        status=event["status"],
        duration_ms=event["duration_ms"],
        channel=event["channel"],
        subject_id=event["subject_id"],
        summary=event["summary"],
        error_code=event["error_code"],
        counts=event["counts"],
        payload=event["payload"],
        correlation_id=event["correlation_id"],
    )


def _make_digest_from_values(
    *,
    timestamp: str,
    event_name: str,
    status: str,
    duration_ms: Optional[float],
    channel: str,
    subject_id: Optional[str],
    summary: Optional[str],
    error_code: Optional[str],
    counts: Dict[str, int],
    payload: Dict[str, JSONValue],
    correlation_id: Optional[str],
) -> LogEventDigest:
    count_keys = _digest_count_keys(counts)
    limited_counts = {key: counts[key] for key in count_keys}
    fields: List[Tuple[str, str]] = []
    for name, value in (
        ("correlation_id", correlation_id),
        ("subject_id", subject_id),
        ("error_code", error_code),
        ("duration_ms", duration_ms),
    ):
        if value is not None:
            fields.append((name, str(value)))
    for key in count_keys:
        fields.append((f"count.{key}", str(counts[key])))
    for key in PAYLOAD_FIELD_ORDER:
        value = payload.get(key)
        if key == "error_code" and error_code is not None and value == error_code:
            continue
        if _is_scalar_for_digest(value):
            fields.append((key, str(value)))
    return LogEventDigest(
        timestamp=timestamp,
        event_name=event_name,
        status=status,
        duration_ms=duration_ms,
        channel=channel,
        subject_id=subject_id,
        summary=summary,
        error_code=error_code,
        counts=limited_counts,
        fields=fields[:DIGEST_FIELD_LIMIT],
    )


def _digest_count_keys(counts: Dict[str, int]) -> List[str]:
    selected: List[str] = []
    for key in DIGEST_PRIORITY_COUNT_FIELDS:
        if counts.get(key, 0) != 0 or key in ALWAYS_DIGEST_ZERO_COUNT_FIELDS and key in counts:
            selected.append(key)
    for key in sorted(counts):
        if key in selected:
            continue
        selected.append(key)
        if len(selected) >= DIGEST_COUNT_FIELD_LIMIT:
            break
    return selected[:DIGEST_COUNT_FIELD_LIMIT]


def _is_scalar_for_digest(value: Any) -> bool:
    return value is not None and isinstance(value, (str, int, float, bool))


def _keep_recent(items: List[Tuple[str, int, LogEventDigest]], candidate: Tuple[str, int, LogEventDigest], limit: int) -> None:
    items.append(candidate)
    items.sort(key=lambda item: (item[0], item[1]))
    if len(items) > limit:
        del items[0]


def _keep_slow(
    items: List[Tuple[float, str, int, LogEventDigest]],
    candidate: Tuple[float, str, int, LogEventDigest],
    limit: int,
) -> None:
    items.append(candidate)
    items.sort(key=lambda item: (item[0], item[1], -item[2]), reverse=True)
    if len(items) > limit:
        del items[limit:]


def _count_value(value: JSONValue, expected: str) -> int:
    if value == expected:
        return 1
    if isinstance(value, list):
        return sum(_count_value(item, expected) for item in value)
    if isinstance(value, dict):
        return sum(_count_value(item, expected) for item in value.values())
    return 0


def _count_truncated(value: JSONValue) -> int:
    if isinstance(value, str) and TRUNCATED in value:
        return 1
    if isinstance(value, list):
        return sum(_count_truncated(item) for item in value)
    if isinstance(value, dict):
        return sum(_count_truncated(item) for item in value.values())
    return 0


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


__all__ = [
    "JsonlLog",
    "LogError",
    "LogEvent",
    "LogEventDigest",
    "LogReadIssue",
    "LogReadResult",
    "LogSummary",
    "LogWriteResult",
    "open_log",
    "redact_value",
    "safe_channel_name",
    "truncate_value",
]

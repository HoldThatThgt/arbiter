"""Append-only JSONL log for the absorbed code extractor.

The store's ``_common.open_log`` is a deliberate no-op: the fact store runs
log-disabled because its forensics live in the referee journal. The extractor,
however, is the one cipher-2 subsystem whose own observability is part of its
contract — its acceptance tests read back ``extractor.code.*`` events to assert
diagnostic kinds, worker-pool counts, and payload sanitization. Wiring the
extractor to the store no-op silently dropped those events (a port regression).

This module restores a real jsonl sink, faithfully trimmed from cipher-2's
``cipher2.tools.log`` to exactly the surface the extractor writes and the
acceptance tests read: ``LogEvent`` (+ ``to_json``), ``open_log(target)`` →
``JsonlLog`` with ``write_event`` / ``read_events`` / ``summarize``, and the
redaction + truncation applied on write. The on-disk channel root moves from
cipher-2's ``.cipher/log`` to arbiter's ``.arbiter/facts/log`` (see the M4
migration map §1.6).
"""

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

from arbiter_engine.facts.store._common import JSONValue

# cipher-2's JsonlLog took a POSIX file lock around each append to serialize concurrent
# CLI invocations. In arbiter the extractor is the sole log writer and it runs in the
# main process (workers return results, never emit events), so a per-path threading.Lock
# fully serializes writes. We deliberately do NOT take a raw OS-level file lock here:
# arbiter mandates that every such lock route through arbiter_engine/shared/locks.py
# (enforced by tests/test_locks.py), and a general-purpose append lock is not part of
# that ordered-lock vocabulary. Append-atomicity for the single writer is preserved by
# the open("a") + threading.Lock pairing below.


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

    def write_event(self, event: LogEvent) -> LogWriteResult:
        return self._append_event(event)

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
        latest_event_at: Optional[str] = None
        latest_error_at: Optional[str] = None
        latest_error_code: Optional[str] = None
        paths = self._selected_paths(channel)
        bytes_on_disk = sum(path.stat().st_size for path in paths if path.exists())

        for path in paths:
            for event, issue in self._iter_path(path, since=since, until=until):
                if issue is not None:
                    malformed_lines += 1
                    continue
                if event is None:
                    continue
                total_events += 1
                events_by_channel[event.channel] += 1
                events_by_name[event.event_name] += 1
                events_by_status[event.status] += 1
                if event.error_code:
                    error_codes[event.error_code] += 1
                    if latest_error_at is None or event.timestamp >= latest_error_at:
                        latest_error_at = event.timestamp
                        latest_error_code = event.error_code
                if event.duration_ms is not None:
                    duration_ms_total += float(event.duration_ms)
                for key, value in event.counts.items():
                    custom_counts[key] += value
                if latest_event_at is None or event.timestamp > latest_event_at:
                    latest_event_at = event.timestamp

        return LogSummary(
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
        )

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
            path.parent.mkdir(parents=True, exist_ok=True)
            with lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
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
        base = self.target_repo / ".arbiter" / "facts" / "log"
        path = base / f"{safe}.jsonl"
        base_resolved = base.resolve(strict=False)
        path_resolved = path.resolve(strict=False)
        if not _is_relative_to(path_resolved, base_resolved):
            raise LogError("path_escape", "log path escapes .arbiter/facts/log", path=path)
        return path

    def _selected_paths(self, channel: Optional[str]) -> List[Path]:
        if channel is not None:
            return [self._channel_path(channel)]
        base = (self.target_repo / ".arbiter" / "facts" / "log").resolve(strict=False)
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

    def _record_write_failure(self, path: Path, error: LogError) -> None:
        self.dropped_event_count += 1
        if not self.stderr_reported:
            self.stderr_reported = True
            print(f"arbiter facts log write failed: path={path} error_code={error.code}", file=sys.stderr)


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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _dumps_line(row: Dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


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
    "LogReadIssue",
    "LogReadResult",
    "LogSummary",
    "LogWriteResult",
    "open_log",
    "redact_value",
    "safe_channel_name",
    "truncate_value",
]

"""Minimal real jsonl event log for the incremental coordinator.

The absorbed fact store + extractor run log-disabled (forensics live in the referee
journal, not cipher-2's 1152-LOC tools.log). The incremental coordinator is the one
component whose own audit trail is part of its contract — its tests assert
``log.read_events(channel="incremental")`` — so it gets a real, append-only jsonl sink.
This is a deliberately small subset of cipher-2's log: write_event + channel-filtered
read_events, stdlib-only.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from arbiter_engine.facts import relocation
from arbiter_engine.facts.store._common import JSONValue, LogError

_LOG_DIR = ("run", "log")
_LOG_FILENAME = "incremental.jsonl"


@dataclass(frozen=True)
class LogEvent:
    event_name: str
    channel: str
    status: str = "ok"
    counts: Dict[str, int] = field(default_factory=dict)
    payload: Dict[str, JSONValue] = field(default_factory=dict)
    error_code: Optional[str] = None
    timestamp: Optional[str] = None

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "event_name": self.event_name,
            "channel": self.channel,
            "status": self.status,
            "counts": dict(self.counts),
            "payload": dict(self.payload),
            "error_code": self.error_code,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class LogReadResult:
    events: List[LogEvent] = field(default_factory=list)


class JsonlLog:
    """Append-only jsonl event sink, repo-scoped, thread-safe for a single writer."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write_event(self, event: LogEvent, *, observe: bool = False) -> None:
        stamped = event if event.timestamp else _with_timestamp(event)
        line = json.dumps(stamped.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:  # pragma: no cover - surfaced as LogError, never crashes the caller
            raise LogError(str(exc)) from exc

    def read_events(self, *, channel: Optional[str] = None) -> LogReadResult:
        events: List[LogEvent] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return LogReadResult(events=())
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if channel is not None and row.get("channel") != channel:
                continue
            events.append(
                LogEvent(
                    event_name=str(row.get("event_name", "")),
                    channel=str(row.get("channel", "")),
                    status=str(row.get("status", "ok")),
                    counts=dict(row.get("counts") or {}),
                    payload=dict(row.get("payload") or {}),
                    error_code=row.get("error_code"),
                    timestamp=row.get("timestamp"),
                )
            )
        return LogReadResult(events=events)


def _with_timestamp(event: LogEvent) -> LogEvent:
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return LogEvent(
        event_name=event.event_name,
        channel=event.channel,
        status=event.status,
        counts=event.counts,
        payload=event.payload,
        error_code=event.error_code,
        timestamp=stamp,
    )


def open_log(target_repo: Path) -> JsonlLog:
    return JsonlLog(relocation.facts_dir(Path(target_repo)).joinpath(*_LOG_DIR, _LOG_FILENAME))


__all__ = ["JsonlLog", "LogEvent", "LogReadResult", "LogError", "open_log"]

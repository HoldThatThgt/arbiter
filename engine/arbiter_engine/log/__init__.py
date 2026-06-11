"""Redacted channel logging for the Arbiter engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional


CHANNELS = {"facts", "runs"}
CORRELATION_KEYS = ("match_id", "round", "task_id", "run_id")
SECRET_MARKERS = (
    "api_key",
    "apikey",
    "auth",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
)
RUN_LENGTH_KEYS = {"stdout", "stderr", "output", "payload", "report"}


class ChannelWriter:
    def __init__(self, repo: Path, channel: str) -> None:
        if channel not in CHANNELS:
            raise ValueError(f"unknown log channel {channel!r}")
        self.repo = Path(repo)
        self.channel = channel
        self.path = self.repo / ".arbiter" / "log" / f"{channel}.jsonl"

    def write(
        self,
        event: str,
        payload: Optional[Mapping[str, Any]] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        record = {
            "channel": self.channel,
            "event": event,
            "correlation": _correlation(meta or {}),
            "payload": _redact_value(self.channel, payload or {}, key="payload"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.path.exists()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        if created:
            os.chmod(self.path, 0o600)


def _correlation(meta: Mapping[str, Any]) -> dict[str, Any]:
    return {key: meta[key] for key in CORRELATION_KEYS if key in meta}


def _redact_value(channel: str, value: Any, key: str = "") -> Any:
    if _secret_key(key):
        return _summary(value, redacted=True)

    if isinstance(value, Mapping):
        return {str(child_key): _redact_value(channel, child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(channel, item, key) for item in value]
    if channel == "runs" and key in RUN_LENGTH_KEYS:
        return _summary(value, redacted=False)
    return value


def _summary(value: Any, redacted: bool) -> dict[str, Any]:
    if isinstance(value, str):
        length = len(value)
    elif isinstance(value, (bytes, bytearray)):
        length = len(value)
    else:
        length = len(json.dumps(value, sort_keys=True, separators=(",", ":")))
    out = {"length": length}
    if redacted:
        out["redacted"] = True
    return out


def _secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in SECRET_MARKERS)

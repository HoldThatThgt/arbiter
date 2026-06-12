from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config


class AuditLog:
    def __init__(self, config: Config):
        self._config = config
        self._path = config.root / ".gdb-mcp" / "audit.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        event: str,
        *,
        tool: Optional[str] = None,
        session_id: Optional[str] = None,
        ok: Optional[bool] = None,
        elapsed_ms: Optional[int] = None,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._config.audit:
            return
        payload: Dict[str, Any] = {
            "ts": round(time.time(), 6),
            "event": event,
        }
        if tool:
            payload["tool"] = tool
        if session_id:
            payload["session_id"] = session_id
        if ok is not None:
            payload["ok"] = ok
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        if summary:
            payload["summary"] = _scrub(summary)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("secret", "token", "password", "env")):
                if isinstance(item, dict):
                    out[key] = {"keys": sorted(str(k) for k in item.keys()), "count": len(item)}
                else:
                    out[key] = "<redacted>"
            else:
                out[key] = _scrub(item)
        return out
    if isinstance(value, list):
        if len(value) > 20:
            return [_scrub(item) for item in value[:20]] + [{"truncated": len(value) - 20}]
        return [_scrub(item) for item in value]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "...<truncated>"
    return value


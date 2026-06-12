"""Best-effort init progress events for terminal rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from cipher2.common import JSONValue


@dataclass(frozen=True)
class InitProgressEvent:
    kind: str
    elapsed_ms: float
    source: Optional[str] = None
    total: Optional[int] = None
    counts: Dict[str, int] = field(default_factory=dict)
    payload: Dict[str, JSONValue] = field(default_factory=dict)


InitProgressSink = Callable[[InitProgressEvent], None]


__all__ = ["InitProgressEvent", "InitProgressSink"]

"""Local shims replacing cipher-2 module couplings in the absorbed fact store.

cipher-2's storage imported `JSONValue` from `cipher2.common` and a structured log
sink (`LogError`/`LogEvent`/`open_log`) from `cipher2.tools.log`. The absorbed store
runs log-disabled, so the sink is reduced to no-ops; `JSONValue` is reproduced verbatim.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

JSONValue = Union[None, bool, int, float, str, List["JSONValue"], Dict[str, "JSONValue"]]


class LogError(Exception):
    """Raised by the cipher-2 log sink; retained so call sites stay importable."""


class LogEvent:
    """Inert stand-in for cipher-2's structured log event."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def open_log(*args: Any, **kwargs: Any) -> Optional["_NullLog"]:
    """The absorbed store runs with logging disabled — return a no-op sink."""
    return _NullLog()


class _NullLog:
    def write(self, *args: Any, **kwargs: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> "_NullLog":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


__all__ = ["JSONValue", "LogError", "LogEvent", "open_log"]

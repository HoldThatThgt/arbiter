"""Local shims replacing cipher-2 module couplings in the absorbed code extractor.

The ported extractor imported four things from the wider cipher-2 runtime that do
not exist in arbiter:

  * ``cipher2.config.CipherConfig`` — the persisted-config dataclass. The extractor
    reads exactly six of its attributes; the rest of CipherConfig (snapshot dirs,
    incremental knobs, YAML loaders) is initializer plumbing the extractor never
    touches. We reproduce only the six fields it reads, plus sensible defaults, as a
    frozen ``ExtractorConfig`` and alias it to the name the ported code references.
  * ``cipher2.initializer.progress.{InitProgressEvent, InitProgressSink}`` — the
    best-effort terminal progress channel. ``InitProgressEvent`` is reproduced
    verbatim (the extractor constructs it by keyword). ``InitProgressSink`` is a
    callable type alias in cipher-2; the extractor takes it ``Optional`` and guards
    on ``None``, so the no-op default is simply ``None`` and a no-op callable class
    is provided for callers that want one.
  * ``cipher2.initializer.InitError`` — the structured initializer error the extractor
    raises through ``_make_init_error``. Reproduced verbatim (code/message/source/
    details + ``__reduce__`` so it survives the ProcessPool round-trip).
  * ``cipher2.incremental.IncrementalBuildResult`` — the Phase-2 dirty-source return
    shape. Reproduced verbatim; only needed so ``extract_dirty_sources`` imports and
    returns it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from arbiter_engine.facts.store import FactRecord, FactRelative, SourceInventoryEntry
from arbiter_engine.facts.store._common import JSONValue


# --- config ---------------------------------------------------------------
#
# Mirrors the six fields cipher2.config.CipherConfig exposes that the extractor
# reads: compile_database_path, clang_executable, libclang_library_path,
# gcc_executable, clang_args, extractor_worker_count. Types/defaults follow
# cipher2/config/__init__.py (libclang_library_path is Optional[Path], clang_args
# is a list[str], extractor_worker_count is int).


@dataclass(frozen=True)
class ExtractorConfig:
    compile_database_path: Optional[Path] = None
    clang_executable: Optional[str] = None
    libclang_library_path: Optional[Path] = None
    gcc_executable: Optional[str] = None
    clang_args: Sequence[str] = ()
    extractor_worker_count: int = 1


# The ported modules import the symbol under cipher-2's name.
CipherConfig = ExtractorConfig


# --- progress -------------------------------------------------------------
#
# Verbatim from cipher2.initializer.progress. The extractor builds these by
# keyword (kind, elapsed_ms, source, total, counts, payload).


@dataclass(frozen=True)
class InitProgressEvent:
    kind: str
    elapsed_ms: float
    source: Optional[str] = None
    total: Optional[int] = None
    counts: Dict[str, int] = field(default_factory=dict)
    payload: Dict[str, JSONValue] = field(default_factory=dict)


# In cipher-2 this is `Callable[[InitProgressEvent], None]`. The extractor only
# ever calls `self.progress_sink(event)` and guards on `None`, so any callable
# satisfies it. Kept as the callable type alias for type annotations.
InitProgressSink = Callable[[InitProgressEvent], None]


class NullProgressSink:
    """A no-op sink for callers that want a non-None, never-failing channel."""

    def __call__(self, event: InitProgressEvent) -> None:
        return None


# --- init error -----------------------------------------------------------
#
# Verbatim from cipher2.initializer.InitError, including __reduce__ so the error
# survives pickling across the ProcessPoolExecutor worker boundary.


class InitError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        source: Optional[str] = None,
        details: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.source = source
        self.details = dict(details or {})

    def __reduce__(self):
        return (
            self.__class__,
            (self.code, self.message),
            {"source": self.source, "details": self.details},
        )


# --- incremental ----------------------------------------------------------
#
# Verbatim from cipher2.incremental.IncrementalBuildResult (Phase 2). Only the
# import + return shape is exercised here.


@dataclass(frozen=True)
class IncrementalBuildResult:
    facts: List[FactRecord] = field(default_factory=list)
    relatives: List[FactRelative] = field(default_factory=list)
    source_inventory: List[SourceInventoryEntry] = field(default_factory=list)


__all__ = [
    "CipherConfig",
    "ExtractorConfig",
    "InitProgressEvent",
    "InitProgressSink",
    "NullProgressSink",
    "InitError",
    "IncrementalBuildResult",
]

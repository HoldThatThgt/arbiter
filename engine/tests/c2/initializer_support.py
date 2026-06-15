"""Test-support shims for the cipher-2 initializer orchestration.

The M4 absorption pulled cipher-2's AST extractor (``CodeFactExtractor``) into
``arbiter_engine.facts.extractor.code`` but deliberately left the thin
``cipher2.initializer`` orchestrator behind: arbiter drives extraction through
its own facts pipeline, not a ``initialize_repository`` entry point (migration
map §1.5 — "cipher2's initializer top-level orchestration maps to
``CodeFactExtractor`` (collect/stream/extract)").

cipher-2's own acceptance tests, however, exercise that orchestrator directly.
To run them as acceptance tests we reproduce the orchestrator *as test support*
(not engine code): a faithful, line-for-line transcription of
``cipher2.initializer.initialize_repository`` + ``InitSummary``, re-pointed at the
absorbed extractor, the arbiter fact store, and the extractor's real jsonl log.
``InitError`` is re-exported from the extractor's ``_shim`` so ``assertRaises``
catches exactly the error the extractor raises through ``_make_init_error``.

Also home to ``build_config`` — the ``write_default_config`` analog. cipher-2's
``write_default_config`` persisted a ``config.yml`` that ``load_config`` later
resolved; arbiter has no config file (the config subsystem is excluded, map §3),
so ``build_config`` resolves the same inputs straight into the 6-field
``ExtractorConfig`` the extractor consumes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from arbiter_engine.facts.store import StorageError, open_fact_store
from arbiter_engine.facts.extractor.code import CodeFactExtractor
from arbiter_engine.facts.extractor.code._log import LogError, LogEvent, open_log
from arbiter_engine.facts.extractor.code._shim import ExtractorConfig, InitError, InitProgressSink


DEFAULT_PROFILE = "default"


def build_config(
    target: Path,
    *,
    compile_database: Optional[Union[str, Path]] = None,
    clang_executable: Optional[Union[str, Path]] = None,
    gcc_executable: Optional[Union[str, Path]] = None,
    libclang_library: Optional[Union[str, Path]] = None,
    clang_args: Optional[Sequence[str]] = None,
    extractor_worker_count: int = 1,
) -> ExtractorConfig:
    """The ``write_default_config`` analog: resolve inputs into an ExtractorConfig.

    Relative executable / database paths are resolved against ``target`` exactly
    as cipher-2's ``load_config`` did, so the absorbed extractor sees absolute
    paths and resolves the toolchain the same way.
    """

    target = Path(target)
    return ExtractorConfig(
        compile_database_path=_resolve_path(target, compile_database),
        clang_executable=_resolve_tool(target, clang_executable),
        libclang_library_path=_resolve_path(target, libclang_library),
        gcc_executable=_resolve_tool(target, gcc_executable),
        clang_args=tuple(clang_args or ()),
        extractor_worker_count=extractor_worker_count,
    )


# cipher-2's tests called ``write_default_config(target, ...)`` to persist a config and later
# ``load_config(target, ...)`` to read it back; arbiter has no config file. We reproduce that
# write-then-read contract in-process: ``write_default_config`` stashes the built ExtractorConfig
# keyed by resolved target, and ``load_config`` returns it (applying any worker-count override the
# way cipher-2's ``overrides={"extractor": {"worker_count": N}}`` did).
_CONFIG_BY_TARGET: Dict[str, ExtractorConfig] = {}


def write_default_config(
    target: Path,
    *,
    compile_database: Optional[Union[str, Path]] = None,
    clang_executable: Optional[Union[str, Path]] = None,
    gcc_executable: Optional[Union[str, Path]] = None,
    libclang_library: Optional[Union[str, Path]] = None,
    clang_args: Optional[Sequence[str]] = None,
    extractor_worker_count: Optional[int] = None,
    observe: bool = False,
) -> ExtractorConfig:
    config = build_config(
        target,
        compile_database=compile_database,
        clang_executable=clang_executable,
        gcc_executable=gcc_executable,
        libclang_library=libclang_library,
        clang_args=clang_args,
        extractor_worker_count=1 if extractor_worker_count is None else extractor_worker_count,
    )
    _CONFIG_BY_TARGET[str(Path(target).resolve(strict=False))] = config
    return config


def load_config(
    target: Path,
    *,
    overrides: Optional[Dict[str, Dict[str, int]]] = None,
    observe: bool = False,
) -> ExtractorConfig:
    from dataclasses import replace

    key = str(Path(target).resolve(strict=False))
    config = _CONFIG_BY_TARGET.get(key)
    if config is None:
        config = build_config(target)
    worker_count = (overrides or {}).get("extractor", {}).get("worker_count")
    if worker_count is not None:
        config = replace(config, extractor_worker_count=worker_count)
    return config


def _resolve_path(target: Path, value: Optional[Union[str, Path]]) -> Optional[Path]:
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else (target / path)


def _resolve_tool(target: Path, value: Optional[Union[str, Path]]) -> Optional[str]:
    if value is None:
        return None
    path = Path(value)
    return str(path if path.is_absolute() else (target / path))


@dataclass(frozen=True)
class InitSummary:
    ok: bool
    snapshot_id: Optional[str]
    fact_count: int
    relative_count: int
    facts_by_kind: Dict[str, int]
    relatives_by_kind: Dict[str, int]
    source_count: int
    warning_count: int
    errors: List[InitError] = field(default_factory=list)
    duration_ms: float = 0.0


def initialize_repository(
    target_repo: Path,
    *,
    config: Optional[ExtractorConfig] = None,
    source_roots: Optional[List[Union[str, Path]]] = None,
    profile: Optional[str] = None,
    log_enabled: Optional[bool] = None,
    progress_sink: Optional[InitProgressSink] = None,
) -> InitSummary:
    target = Path(target_repo)
    started = time.perf_counter()
    observe = _normalize_log_enabled(log_enabled)
    normalized_profile = _normalize_profile(profile)
    if config is None:
        config = build_config(target)
    try:
        extractor = CodeFactExtractor(target, config, log_enabled=observe, progress_sink=progress_sink)
        with extractor.stream(source_roots, normalized_profile) as extraction:
            manifest = open_fact_store(target, mode="w", log_enabled=False)._replace_snapshot_preencoded_sorted_unique(
                extraction.encoded_facts,
                extraction.encoded_relatives,
                extraction.source_inventory,
            )
            facts_by_kind = extraction.facts_by_kind
            relatives_by_kind = extraction.relatives_by_kind
            source_count = extraction.source_count
            warning_count = len(extraction.errors)
            errors = list(extraction.errors)
        summary = InitSummary(
            ok=True,
            snapshot_id=manifest.snapshot_id,
            fact_count=manifest.fact_count,
            relative_count=manifest.relative_count,
            facts_by_kind=facts_by_kind,
            relatives_by_kind=relatives_by_kind,
            source_count=source_count,
            warning_count=warning_count,
            errors=errors,
            duration_ms=_elapsed_ms(started),
        )
        _emit_initializer_run(target, summary, normalized_profile, observe, started)
        return summary
    except InitError as exc:
        _emit_initializer_error(target, exc.code, observe, started)
        raise
    except StorageError as exc:
        error = InitError("storage_error", "failed to write initializer facts", details={"storage_code": exc.code})
        _emit_initializer_error(target, error.code, observe, started)
        raise error from exc


def _normalize_profile(profile: Optional[str]) -> str:
    if profile is None:
        return DEFAULT_PROFILE
    if not isinstance(profile, str) or not profile.strip():
        raise InitError("invalid_profile", "profile must be a non-empty string")
    return profile


def _normalize_log_enabled(log_enabled: Optional[bool]) -> bool:
    if log_enabled is None:
        return True
    if not isinstance(log_enabled, bool):
        raise InitError("invalid_log_enabled", "log_enabled must be a bool or None")
    return log_enabled


def _emit_initializer_run(
    target: Path,
    summary: InitSummary,
    profile: str,
    observe: bool,
    started: float,
) -> None:
    if not observe:
        return
    _write_initializer_event(
        target,
        LogEvent(
            event_name="initializer.run",
            channel="initializer",
            status="ok",
            duration_ms=_elapsed_ms(started),
            summary=f"initialized {summary.fact_count} facts",
            counts={
                "fact_count": summary.fact_count,
                "relative_count": summary.relative_count,
                "source_count": summary.source_count,
                "warning_count": summary.warning_count,
            },
            payload={
                "operation": "initialize_repository",
                "outcome": "written",
                "snapshot_id": summary.snapshot_id,
                "profile": profile,
            },
        ),
    )


def _emit_initializer_error(target: Path, code: str, observe: bool, started: float) -> None:
    if not observe:
        return
    _write_initializer_event(
        target,
        LogEvent(
            event_name="initializer.error",
            channel="initializer",
            status="error",
            error_code=code,
            duration_ms=_elapsed_ms(started),
            summary=f"initialize_repository failed: {code}",
            payload={
                "operation": "initialize_repository",
                "outcome": "failed",
                "error_code": code,
            },
        ),
    )


def _write_initializer_event(target: Path, event: LogEvent) -> None:
    try:
        open_log(target).write_event(event)
    except LogError:
        pass


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000)


__all__ = [
    "DEFAULT_PROFILE",
    "InitError",
    "InitSummary",
    "build_config",
    "load_config",
    "write_default_config",
    "initialize_repository",
]

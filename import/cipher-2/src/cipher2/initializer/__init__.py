"""Repository initialization runtime for cipher-2."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

from cipher2.common import JSONValue
from cipher2.config import ConfigError, load_config
from cipher2.initializer.extractor.code import CodeFactExtractor, ExtractionResult
from cipher2.initializer.progress import InitProgressSink
from cipher2.storage import StorageError, open_fact_store
from cipher2.tools.log import LogError, LogEvent, open_log


DEFAULT_PROFILE = "default"
INIT_STAGE_ORDER = (
    "collect",
    "extract",
    "reduce",
    "resolve",
    "relative_merge",
    "snapshot_write",
    "read_index",
)


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


@dataclass(frozen=True)
class InitStageTiming:
    stage: str
    duration_ms: float
    counts: Dict[str, int] = field(default_factory=dict)
    payload: Dict[str, JSONValue] = field(default_factory=dict)

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "stage": self.stage,
            "duration_ms": self.duration_ms,
            "counts": dict(self.counts),
            "payload": dict(self.payload),
        }


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
    stage_timings: List[InitStageTiming] = field(default_factory=list)


@dataclass(frozen=True)
class BuildReadinessReport:
    ok: bool
    compile_database_path: Optional[Path]
    clang_ready: bool
    gcc_ready: bool
    missing_inputs: List[str] = field(default_factory=list)
    errors: List[InitError] = field(default_factory=list)


def initialize_repository(
    target_repo: Path,
    *,
    source_roots: Optional[List[Union[str, Path]]] = None,
    profile: Optional[str] = None,
    log_enabled: Optional[bool] = None,
    progress_sink: Optional[InitProgressSink] = None,
) -> InitSummary:
    target = Path(target_repo)
    started = time.perf_counter()
    observe = _normalize_log_enabled(log_enabled)
    normalized_profile = _normalize_profile(profile)
    stage_recorder = _InitStageRecorder(target, observe)
    try:
        config = load_config(target, observe=observe)
        extractor = CodeFactExtractor(
            target,
            config,
            log_enabled=observe,
            progress_sink=progress_sink,
            stage_sink=stage_recorder.record,
        )
        with extractor.stream(source_roots, normalized_profile) as extraction:
            manifest = open_fact_store(
                target,
                mode="w",
                log_enabled=observe,
                stage_sink=stage_recorder.record,
            )._replace_snapshot_preencoded_sorted_unique(
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
            stage_timings=stage_recorder.timings,
        )
        _emit_initializer_run(target, summary, normalized_profile, observe, started)
        return summary
    except ConfigError as exc:
        _emit_initializer_error(target, exc.code, observe, started)
        raise
    except InitError as exc:
        _emit_initializer_error(target, exc.code, observe, started)
        raise
    except StorageError as exc:
        error = InitError("storage_error", "failed to write initializer facts", details={"storage_code": exc.code})
        _emit_initializer_error(target, error.code, observe, started)
        raise error from exc


def preflight_build_readiness(target_repo: Path, *, log_enabled: Optional[bool] = None) -> BuildReadinessReport:
    target = Path(target_repo)
    started = time.perf_counter()
    observe = _normalize_log_enabled(log_enabled)
    try:
        config = load_config(target, observe=observe)
    except ConfigError as exc:
        error = InitError(exc.code, exc.message)
        report = BuildReadinessReport(
            ok=False,
            compile_database_path=None,
            clang_ready=False,
            gcc_ready=False,
            missing_inputs=[exc.code],
            errors=[error],
        )
        _emit_build_readiness(target, report, observe, started)
        raise
    report = BuildReadinessReport(
        ok=True,
        compile_database_path=config.compile_database_path,
        clang_ready=True,
        gcc_ready=True,
        missing_inputs=[],
        errors=[],
    )
    _emit_build_readiness(target, report, observe, started)
    return report


def estimate_initializer_peak_bytes(
    *,
    max_file_bytes: int,
    fact_count: int,
    relative_count: int = 0,
    function_fact_count: Optional[int] = None,
    staging_window_count: int = 0,
    average_fact_bytes: int,
    average_relative_bytes: Optional[int] = None,
    streaming_write: bool,
    safety_margin_bytes: int,
) -> int:
    for name, value in (
        ("max_file_bytes", max_file_bytes),
        ("fact_count", fact_count),
        ("relative_count", relative_count),
        ("staging_window_count", staging_window_count),
        ("average_fact_bytes", average_fact_bytes),
        ("safety_margin_bytes", safety_margin_bytes),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise InitError("invalid_estimate_input", f"{name} must be a non-negative integer")
    relative_bytes = average_fact_bytes if average_relative_bytes is None else average_relative_bytes
    if not isinstance(relative_bytes, int) or isinstance(relative_bytes, bool) or relative_bytes < 0:
        raise InitError("invalid_estimate_input", "average_relative_bytes must be a non-negative integer")
    if function_fact_count is not None and (
        not isinstance(function_fact_count, int) or isinstance(function_fact_count, bool) or function_fact_count < 0
    ):
        raise InitError("invalid_estimate_input", "function_fact_count must be a non-negative integer")
    if streaming_write:
        retained_fact_count = fact_count if function_fact_count is None else function_fact_count
        fact_buffer = (retained_fact_count + staging_window_count) * average_fact_bytes
        relative_buffer = staging_window_count * relative_bytes
    else:
        fact_buffer = fact_count * average_fact_bytes
        relative_buffer = relative_count * relative_bytes
    return max_file_bytes + fact_buffer + relative_buffer + safety_margin_bytes


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


class _InitStageRecorder:
    def __init__(self, target: Path, observe: bool) -> None:
        self.target = target
        self.observe = observe
        self._timings: List[InitStageTiming] = []

    @property
    def timings(self) -> List[InitStageTiming]:
        return list(self._timings)

    def record(
        self,
        stage: str,
        duration_ms: float,
        counts: Optional[Dict[str, int]] = None,
        payload: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        if stage not in INIT_STAGE_ORDER:
            return
        normalized_duration = max(0.0, float(duration_ms))
        clean_counts = _stage_counts(counts or {})
        clean_payload = _stage_payload(payload or {})
        timing = InitStageTiming(
            stage=stage,
            duration_ms=normalized_duration,
            counts=clean_counts,
            payload=clean_payload,
        )
        self._timings.append(timing)
        if not self.observe:
            return
        event_payload: Dict[str, JSONValue] = {
            "operation": "initialize_repository",
            "outcome": "stage_completed",
            "stage": stage,
            "stage_duration_ms": round(normalized_duration),
            **clean_payload,
        }
        _write_initializer_event(
            self.target,
            LogEvent(
                event_name="init.stage",
                channel="initializer",
                status="ok",
                duration_ms=normalized_duration,
                summary=f"init stage {stage} completed",
                counts=clean_counts,
                payload=event_payload,
            ),
        )


def _stage_counts(counts: Dict[str, int]) -> Dict[str, int]:
    clean: Dict[str, int] = {}
    for key, value in counts.items():
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool):
            clean[key] = value
    return clean


def _stage_payload(payload: Dict[str, JSONValue]) -> Dict[str, JSONValue]:
    clean: Dict[str, JSONValue] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
    return clean


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


def _emit_build_readiness(target: Path, report: BuildReadinessReport, observe: bool, started: float) -> None:
    if not observe:
        return
    _write_initializer_event(
        target,
        LogEvent(
            event_name="initializer.build_readiness",
            channel="initializer",
            status="ok" if report.ok else "error",
            error_code=None if report.ok else (report.errors[0].code if report.errors else "build_readiness_failed"),
            duration_ms=_elapsed_ms(started),
            counts={
                "missing_input_count": len(report.missing_inputs),
                "warning_count": 0,
            },
            payload={
                "operation": "build_readiness",
                "outcome": "ready" if report.ok else "failed",
                "has_compile_database": report.compile_database_path is not None,
                "clang_ready": report.clang_ready,
                "gcc_ready": report.gcc_ready,
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
    "BuildReadinessReport",
    "InitError",
    "InitStageTiming",
    "InitSummary",
    "ExtractionResult",
    "estimate_initializer_peak_bytes",
    "initialize_repository",
    "preflight_build_readiness",
]

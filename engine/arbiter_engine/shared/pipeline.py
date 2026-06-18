"""Build-driven facts indexing pipeline."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from arbiter_engine.facts import relocation
from arbiter_engine.facts.extractor.code import CodeFactExtractor
# TOOLCHAIN_FAILURE_CODES (the codes that make a broken indexer a hard stop) is defined next to
# InitError in _shim — the single source of truth shared by this module, facts.incremental, and
# facts.view. Re-exported here so existing callers (gtest.run_target, tests) keep using it.
from arbiter_engine.facts.extractor.code._shim import ExtractorConfig, InitError, TOOLCHAIN_FAILURE_CODES
from arbiter_engine.facts.store import FileFactStore
from arbiter_engine.shared import compile_db


@dataclass(frozen=True)
class PipelineResult:
    published: bool
    snapshot_id: Optional[str]
    files: int
    warnings: list[Mapping[str, Any]]
    extract_ms: int
    hidden_ms: int
    tail_ms: int

    def to_json(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "snapshot_id": self.snapshot_id,
            "files": self.files,
            "warnings": list(self.warnings),
            "extract_ms": self.extract_ms,
            "hidden_ms": self.hidden_ms,
            "tail_ms": self.tail_ms,
        }


def publish_after_build(
    repo_root: Path | str,
    journals: Sequence[Path | str],
    compile_db_path: Path | str,
    *,
    profile: str = "default",
    extractor_config: Optional["ExtractorConfig"] = None,
    key_flags: Iterable[str] = (),
    pool: Optional[int] = None,
    build_succeeded: bool = True,
    lock_timeout_s: float = 30.0,
    cpu_count: Callable[[], Optional[int]] = os.cpu_count,
    monotonic: Callable[[], float] = time.monotonic,
) -> PipelineResult:
    root = Path(repo_root)
    tail_start = monotonic()
    records = list(_read_records(journals))
    tail_ms = _elapsed_ms(tail_start, monotonic)
    # A miss marks a single TU whose compile command arbiter cc could not capture — most
    # often a transient fork/exec failure under a parallel build, not a compile error (the
    # build's own success is verified separately just below). compile_db.emit already drops
    # missed TUs, so one flaky fork must not invalidate the whole snapshot: abort only when
    # NOTHING journaled cleanly; otherwise publish facts from the TUs that did, and report
    # the skipped count as a warning.
    missed = sum(1 for record in records if record.get("miss") is True)
    if missed and missed == len(records):
        return PipelineResult(
            published=False,
            snapshot_id=None,
            files=0,
            warnings=[{"kind": "journal_miss", "message": "compile journal contains only miss markers"}],
            extract_ms=0,
            hidden_ms=0,
            tail_ms=tail_ms,
        )
    partial_miss_warnings: list[dict[str, Any]] = (
        [{"kind": "journal_miss_partial", "message": f"{missed} translation unit(s) skipped: compile command not journaled"}]
        if missed
        else []
    )
    if not build_succeeded:
        return PipelineResult(
            published=False,
            snapshot_id=None,
            files=0,
            warnings=[{"kind": "build_failed", "message": "build did not complete green"}],
            extract_ms=0,
            hidden_ms=0,
            tail_ms=tail_ms,
        )

    compile_db.emit(journals, compile_db_path)
    workers = pool if isinstance(pool, int) and pool > 0 else (cpu_count() or 1)
    # Production auto-detects the toolchain (clang/libclang via toolchain.py) unless
    # .arbiter/config.yml facts.toolchain pins it (indexer-only); tests inject a
    # fake-toolchain ExtractorConfig. Either way the compile-db is the journaled one just emitted.
    if extractor_config is not None:
        config = extractor_config
    else:
        config = ExtractorConfig(
            compile_database_path=Path(compile_db_path),
            extractor_worker_count=workers,
            **relocation.extractor_toolchain_overrides(root),
        )
    extract_start = monotonic()
    try:
        # With the compile-db set, collect(None) extracts exactly the journaled/compiled TUs
        # (the compile-db source set), not the whole repo. FileFactStore owns its own write
        # lock, so the pipeline does not also hold locks.SNAPSHOT here.
        result = CodeFactExtractor(root, config).collect(None, profile)
    except InitError as exc:
        if exc.code in TOOLCHAIN_FAILURE_CODES:
            # Mandatory-index hard stop: the indexer toolchain itself is unusable. Propagate so the
            # caller aborts the run with a typed indexer_unavailable failure rather than publishing
            # nothing and letting the build/run report green without a fact index behind it.
            raise
        # A non-toolchain init failure (malformed compile-db, bad source root): the toolchain works
        # but there is nothing to index. Stay a typed not-published signal — builds, runs, shell/mcp
        # predicates, and diagnostics keep working without a facts index.
        extract_ms = _elapsed_ms(extract_start, monotonic)
        return PipelineResult(
            published=False,
            snapshot_id=None,
            files=0,
            warnings=[_extract_error_warning(exc)],
            extract_ms=extract_ms,
            hidden_ms=0,
            tail_ms=tail_ms,
        )
    extract_ms = _elapsed_ms(extract_start, monotonic)

    facts = [fact.to_fact_record() for fact in result.facts]
    manifest = FileFactStore(root, mode="w", log_enabled=False).replace_snapshot(
        facts, result.relatives, result.source_inventory
    )
    # Persist the published compile-db at a stable, recipe-independent location so the incremental
    # coordinator's reconcile (facts/view.py, facts/incremental.py) can re-extract dirty sources with
    # the same flags the build used — the recipe's compile_db.path is not visible to the facts layer.
    _persist_compile_db(root, Path(compile_db_path))
    return PipelineResult(
        published=True,
        snapshot_id=manifest.snapshot_id,
        files=manifest.source_count,
        warnings=partial_miss_warnings + [_extract_error_warning(error) for error in result.errors],
        extract_ms=extract_ms,
        hidden_ms=min(extract_ms, tail_ms),
        tail_ms=tail_ms,
    )


def _persist_compile_db(root: Path, compile_db_path: Path) -> None:
    try:
        data = Path(compile_db_path).read_bytes()
    except OSError:
        return
    dest = relocation.persisted_compile_db_path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)


def _extract_error_warning(error: Exception) -> Mapping[str, Any]:
    return {
        "kind": getattr(error, "code", "extract_failed"),
        "message": str(getattr(error, "message", error)),
    }


def pool_width(cpu_total: int, *, compiler_active: bool, cap: Optional[int] = None) -> int:
    cpu_total = max(1, int(cpu_total))
    width = max(1, cpu_total // 4) if compiler_active else cpu_total
    if cap is not None and cap > 0:
        width = min(width, cap)
    return width


def _read_records(journals: Sequence[Path | str]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for journal in journals:
        path = Path(journal)
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    return records


def _has_miss_marker(records: Sequence[Mapping[str, Any]]) -> bool:
    return any(record.get("miss") is True for record in records)


def _elapsed_ms(start: float, monotonic: Callable[[], float]) -> int:
    return max(0, int(round((monotonic() - start) * 1000)))


__all__ = ["PipelineResult", "pool_width", "publish_after_build"]

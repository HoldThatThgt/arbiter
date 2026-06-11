"""Build-driven facts indexing pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent import futures
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from arbiter_engine.facts import extract_cache
from arbiter_engine.facts import relocation
from arbiter_engine.shared import compile_db
from arbiter_engine.shared import locks


Extractor = Callable[[extract_cache.ExtractUnit], Optional[Mapping[str, Any]]]


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
    extractor: Optional[Extractor] = None,
    key_flags: Iterable[str] = (),
    build_succeeded: bool = True,
    lock_timeout_s: float = 30.0,
    cpu_count: Callable[[], Optional[int]] = os.cpu_count,
    monotonic: Callable[[], float] = time.monotonic,
) -> PipelineResult:
    root = Path(repo_root)
    tail_start = monotonic()
    records = list(_read_records(journals))
    tail_ms = _elapsed_ms(tail_start, monotonic)
    if _has_miss_marker(records):
        return PipelineResult(
            published=False,
            snapshot_id=None,
            files=0,
            warnings=[{"kind": "journal_miss", "message": "compile journal contains a miss marker"}],
            extract_ms=0,
            hidden_ms=0,
            tail_ms=tail_ms,
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
    units = _units_from_compile_db(compile_db_path)
    cache = _load_cache(_cache_path(root))
    pending = [unit for unit in units if unit.key(key_flags=key_flags) not in cache]

    extract_start = monotonic()
    extracted, warnings = _extract_pending(
        pending,
        extractor or _default_extractor,
        max_workers=pool_width(cpu_count() or 1, compiler_active=False),
        key_flags=tuple(key_flags),
    )
    extract_ms = _elapsed_ms(extract_start, monotonic)
    cache.update(extracted)
    snapshot_id = _snapshot_id(units, key_flags=key_flags)

    with locks.acquire(root, [locks.SNAPSHOT], timeout_s=lock_timeout_s):
        _store_cache(_cache_path(root), cache)
        _publish_snapshot(root, snapshot_id, units, warnings)

    return PipelineResult(
        published=True,
        snapshot_id=snapshot_id,
        files=len(units),
        warnings=warnings,
        extract_ms=extract_ms,
        hidden_ms=min(extract_ms, tail_ms),
        tail_ms=tail_ms,
    )


def pool_width(cpu_total: int, *, compiler_active: bool) -> int:
    cpu_total = max(1, int(cpu_total))
    if compiler_active:
        return max(1, cpu_total // 4)
    return cpu_total


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


def _units_from_compile_db(path: Path | str) -> list[extract_cache.ExtractUnit]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    units: list[extract_cache.ExtractUnit] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        file_value = item.get("file")
        arguments = item.get("arguments")
        if not isinstance(file_value, str) or not _string_list(arguments):
            continue
        source = Path(file_value)
        try:
            content = source.read_bytes()
        except OSError:
            content = b""
        units.append(
            extract_cache.ExtractUnit(
                source=str(source),
                tu_content=content,
                include_closure={},
                flags=arguments,
                toolchain_id=_toolchain_id(arguments),
            )
        )
    return units


def _extract_pending(
    units: Sequence[extract_cache.ExtractUnit],
    extractor: Extractor,
    *,
    max_workers: int,
    key_flags: Sequence[str],
) -> tuple[dict[str, Mapping[str, Any]], list[Mapping[str, Any]]]:
    if not units:
        return {}, []
    extracted: dict[str, Mapping[str, Any]] = {}
    warnings: list[Mapping[str, Any]] = []
    with futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        scheduled = {pool.submit(extractor, unit): unit for unit in units}
        for future in futures.as_completed(scheduled):
            unit = scheduled[future]
            key = unit.key(key_flags=key_flags)
            try:
                payload = future.result() or {}
            except Exception as exc:  # noqa: BLE001 - warnings preserve per-file failures.
                warnings.append({"kind": "extract_failed", "file": unit.source, "message": str(exc)})
                extracted[key] = {"source": unit.source, "failed": True}
                continue
            extracted[key] = {"source": unit.source, "failed": False}
            raw_warnings = payload.get("warnings") if isinstance(payload, Mapping) else None
            if isinstance(raw_warnings, list):
                for warning in raw_warnings:
                    if isinstance(warning, Mapping):
                        warnings.append(dict(warning))
    return extracted, warnings


def _default_extractor(unit: extract_cache.ExtractUnit) -> Mapping[str, Any]:
    return {"source": unit.source, "warnings": []}


def _cache_path(root: Path) -> Path:
    return relocation.facts_dir(root) / "extract-cache" / "index.json"


def _load_cache(path: Path) -> dict[str, Mapping[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cache: dict[str, Mapping[str, Any]] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, Mapping):
            cache[key] = dict(value)
    return cache


def _store_cache(path: Path, cache: Mapping[str, Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _publish_snapshot(
    root: Path,
    snapshot_id: str,
    units: Sequence[extract_cache.ExtractUnit],
    warnings: Sequence[Mapping[str, Any]],
) -> None:
    current = relocation.facts_dir(root) / "snapshots" / "current"
    current.mkdir(parents=True, exist_ok=True)
    manifest = {
        "snapshot_id": snapshot_id,
        "files": [unit.source for unit in units],
        "warnings": list(warnings),
    }
    (current / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _snapshot_id(
    units: Sequence[extract_cache.ExtractUnit],
    *,
    key_flags: Iterable[str],
) -> str:
    digest = hashlib.sha256()
    for unit in sorted(units, key=lambda item: item.source):
        digest.update(unit.source.encode("utf-8"))
        digest.update(b"\0")
        digest.update(unit.key(key_flags=key_flags).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _toolchain_id(arguments: Sequence[str]) -> str:
    if not arguments:
        return ""
    return os.path.basename(arguments[0])


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _elapsed_ms(start: float, monotonic: Callable[[], float]) -> int:
    return max(0, int(round((monotonic() - start) * 1000)))


__all__ = ["PipelineResult", "pool_width", "publish_after_build"]

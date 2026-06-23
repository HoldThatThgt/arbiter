from __future__ import annotations

import base64
import binascii
import ctypes
import ctypes.util
import glob
import heapq
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import FrozenInstanceError, dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

try:
    import resource
except ImportError:  # pragma: no cover - Windows compatibility
    resource = None  # type: ignore[assignment]

from arbiter_engine.facts.store._common import JSONValue
from ._shim import CipherConfig
from ._shim import InitProgressEvent, InitProgressSink
from arbiter_engine.facts.store import (
    EncodedFactLine,
    EncodedRelativeLine,
    FactRecord,
    FactRelative,
    RelativeCondition,
    SourceInventoryEntry,
    StoredFactLine,
    StoredRelativeLine,
)
from arbiter_engine.facts.log import LogError, LogEvent, open_log

from .constants import *
from .models import *
from .mapper_utils import *
from .compile_db import *
from . import ast_backend as _ast_backend_module
from .ast_backend import *
from .mapper import _ClangAstMapper
from .direct_calls import (
    _direct_call_evidence_from_json,
    _direct_call_evidence_to_json,
    _direct_call_function_canonical_source,
    _direct_call_function_from_code_fact,
    _make_resolved_direct_call_relative,
    _merge_direct_call_stats,
    _passthrough_ratio_percent,
    _resolve_pending_direct_calls,
    _select_direct_call_target,
)

_PROCESS_FILE_WORKER_EXTRACTOR: Optional["CodeFactExtractor"] = None
_PROCESS_FILE_WORKER_HEADER_CACHE: Optional[_HeaderMaterializationCache] = None
_PROCESS_FILE_WORKER_RELATIVE_DEDUPER: Optional["_WorkerRelativeDeduper"] = None


@dataclass(frozen=True)
class _WorkerRelativeDedupStats:
    relative_map_input_count: int = 0
    relative_map_written_count: int = 0
    relative_map_skipped_exact_count: int = 0
    relative_worker_duplicate_exact_count: int = 0
    relative_worker_duplicate_conflict_count: int = 0
    relative_worker_dedup_tracked_entry_count: int = 0
    relative_worker_dedup_saturated_count: int = 0


@dataclass(frozen=True)
class _RelativeSegmentWriteResult:
    relative_line_bytes: int
    relative_index_bytes: int
    relative_count: int
    relation_kind_counts: Dict[str, int]
    conditional_relative_count: int
    condition_kind_count: int
    dedup_stats: _WorkerRelativeDedupStats


class _WorkerRelativeDeduper:
    def __init__(self, *, max_tracked_bytes: int = WORKER_RELATIVE_DEDUP_MAX_ESTIMATED_BYTES) -> None:
        self._seen: Dict[str, Tuple[int, str]] = {}
        self._max_tracked_bytes = max(0, int(max_tracked_bytes))
        self._tracked_bytes_estimate = 0
        self._saturated = False
        self._saturated_count = 0
        self._input_count = 0
        self._written_count = 0
        self._skipped_exact_count = 0
        self._conflict_count = 0

    def snapshot(self) -> _WorkerRelativeDedupStats:
        return _WorkerRelativeDedupStats(
            relative_map_input_count=self._input_count,
            relative_map_written_count=self._written_count,
            relative_map_skipped_exact_count=self._skipped_exact_count,
            relative_worker_duplicate_exact_count=self._skipped_exact_count,
            relative_worker_duplicate_conflict_count=self._conflict_count,
            relative_worker_dedup_tracked_entry_count=len(self._seen),
            relative_worker_dedup_saturated_count=self._saturated_count,
        )

    def should_write(self, relative: EncodedRelativeLine) -> bool:
        line_text = relative.read_line_text()
        line_byte_count = len(line_text.encode("utf-8"))
        line_fingerprint = (line_byte_count, _hash_text(line_text))
        self._input_count += 1
        existing = self._seen.get(relative.relative_id)
        if existing is not None:
            if existing == line_fingerprint:
                self._skipped_exact_count += 1
                return False
            self._conflict_count += 1
            raise _make_init_error(
                "map_reduce_conflict",
                "duplicate relative id has non-idempotent payload",
                details={"relative_id": relative.relative_id},
            )
        self._track(relative.relative_id, line_fingerprint)
        self._written_count += 1
        return True

    def _track(self, relative_id: str, line_fingerprint: Tuple[int, str]) -> None:
        entry_bytes = (
            len(relative_id.encode("utf-8"))
            + len(line_fingerprint[1].encode("ascii"))
            + 8
            + WORKER_RELATIVE_DEDUP_ENTRY_OVERHEAD_BYTES
        )
        if self._tracked_bytes_estimate + entry_bytes <= self._max_tracked_bytes:
            self._seen[relative_id] = line_fingerprint
            self._tracked_bytes_estimate += entry_bytes
            return
        if not self._saturated:
            self._saturated = True
            self._saturated_count += 1


def _worker_relative_dedup_stats_delta(
    before: _WorkerRelativeDedupStats,
    after: _WorkerRelativeDedupStats,
) -> _WorkerRelativeDedupStats:
    return _WorkerRelativeDedupStats(
        relative_map_input_count=after.relative_map_input_count - before.relative_map_input_count,
        relative_map_written_count=after.relative_map_written_count - before.relative_map_written_count,
        relative_map_skipped_exact_count=after.relative_map_skipped_exact_count - before.relative_map_skipped_exact_count,
        relative_worker_duplicate_exact_count=(
            after.relative_worker_duplicate_exact_count - before.relative_worker_duplicate_exact_count
        ),
        relative_worker_duplicate_conflict_count=(
            after.relative_worker_duplicate_conflict_count - before.relative_worker_duplicate_conflict_count
        ),
        relative_worker_dedup_tracked_entry_count=after.relative_worker_dedup_tracked_entry_count,
        relative_worker_dedup_saturated_count=after.relative_worker_dedup_saturated_count,
    )


def _initialize_process_file_worker(
    target_repo: Path,
    config: CipherConfig,
    backend_spec: _ProcessWorkerBackendSpec,
) -> None:
    global _PROCESS_FILE_WORKER_EXTRACTOR
    global _PROCESS_FILE_WORKER_HEADER_CACHE
    global _PROCESS_FILE_WORKER_RELATIVE_DEDUPER
    if backend_spec.kind == "json_test":
        _install_json_test_libclang_backend()
    from .extractor import CodeFactExtractor

    extractor = CodeFactExtractor(Path(target_repo), config, log_enabled=False)
    if backend_spec.kind == "in_memory_ast":
        if backend_spec.in_memory_ast is None or backend_spec.toolchain_probe_result is None:
            raise RuntimeError("in-memory process backend requires AST and toolchain probe result")
        extractor.toolchain_probe_result = backend_spec.toolchain_probe_result
        extractor._ast_backend = _InMemoryProcessAstBackend(backend_spec.in_memory_ast)
    else:
        extractor._validate_toolchain()
    _PROCESS_FILE_WORKER_EXTRACTOR = extractor
    _PROCESS_FILE_WORKER_HEADER_CACHE = _HeaderMaterializationCache()
    _PROCESS_FILE_WORKER_RELATIVE_DEDUPER = _WorkerRelativeDeduper()


def _run_file_work_item_in_process(item: _FileWorkItem) -> _FileWorkOutcome:
    if (
        _PROCESS_FILE_WORKER_EXTRACTOR is None
        or _PROCESS_FILE_WORKER_HEADER_CACHE is None
        or _PROCESS_FILE_WORKER_RELATIVE_DEDUPER is None
    ):
        raise RuntimeError("process file worker was not initialized")
    return _run_file_work_item_with_cache(
        _PROCESS_FILE_WORKER_EXTRACTOR,
        _PROCESS_FILE_WORKER_HEADER_CACHE,
        item,
        relative_deduper=_PROCESS_FILE_WORKER_RELATIVE_DEDUPER,
        worker_id=os.getpid(),
        publish_header_cache=True,
    )


def _run_file_work_item_with_cache(
    extractor: CodeFactExtractor,
    header_cache: _HeaderMaterializationCache,
    item: _FileWorkItem,
    *,
    relative_deduper: Optional[_WorkerRelativeDeduper] = None,
    worker_id: Optional[int] = None,
    publish_header_cache: bool = False,
) -> _FileWorkOutcome:
    started = time.perf_counter()
    try:
        file_result = extractor._extract_file_work_item(item, header_cache)
    except _RecoverableExtractError as exc:
        return _FileWorkOutcome(
            seq=item.seq,
            source=item.source,
            rel_source=item.rel_source,
            profile=item.profile,
            compile_lookup=item.compile_lookup,
            started=started,
            error_code=exc.code,
            error_message=exc.message,
            diagnostic_kind=exc.diagnostic_kind,
            diagnostic_reason=exc.diagnostic_reason,
            diagnostic_details=dict(exc.details),
            worker_id=worker_id,
            worker_header_cache_entry_count=header_cache.entry_count() if publish_header_cache else None,
        )
    segment_manifest = None
    if item.segment_dir is not None:
        segment_manifest = _write_file_map_segments(item, file_result, relative_deduper=relative_deduper)
        file_result = replace(file_result, facts=[], relatives=[], unresolved_calls=[])
    if publish_header_cache:
        header_cache.publish(
            producer_seq=item.seq,
            context_hash=file_result.header_context_hash,
            keys=file_result.header_decl_keys,
            seed=file_result.header_resolver_seed,
        )
    return _FileWorkOutcome(
        seq=item.seq,
        source=item.source,
        rel_source=item.rel_source,
        profile=item.profile,
        compile_lookup=item.compile_lookup,
        started=started,
        file_result=file_result,
        worker_id=worker_id,
        worker_header_cache_entry_count=header_cache.entry_count() if publish_header_cache else None,
        segment_manifest=segment_manifest,
    )


def _process_worker_backend_spec(extractor: CodeFactExtractor) -> _ProcessWorkerBackendSpec:
    backend = getattr(extractor, "_ast_backend", None)
    ast = getattr(backend, "_ast", None)
    if isinstance(ast, dict) and extractor.toolchain_probe_result is not None:
        return _ProcessWorkerBackendSpec(
            kind="in_memory_ast",
            in_memory_ast=ast,
            toolchain_probe_result=extractor.toolchain_probe_result,
        )
    if _ast_backend_module._TEST_AST_BACKEND_FACTORY is not None:
        return _ProcessWorkerBackendSpec(kind="json_test")
    return _ProcessWorkerBackendSpec(kind="default")


def _write_file_map_segments(
    item: _FileWorkItem,
    result: _FileMapResult,
    *,
    relative_deduper: Optional[_WorkerRelativeDeduper] = None,
) -> _MapSegmentManifest:
    if item.segment_dir is None:
        raise RuntimeError("file work item missing segment directory")
    item.segment_dir.mkdir(parents=True, exist_ok=False)
    facts_path = item.segment_dir / "facts.jsonl"
    relatives_path = item.segment_dir / "relatives.jsonl"
    relatives_index_path = item.segment_dir / "relatives.index"
    unresolved_calls_path = item.segment_dir / "pending_direct_calls.jsonl"
    fact_kind_counts = Counter(fact.fact_kind for fact in result.facts)
    facts_bytes = _write_encoded_fact_rows(facts_path, result.facts)
    relative_write = _write_encoded_relative_rows(
        relatives_path,
        relatives_index_path,
        result.relatives,
        relative_deduper=relative_deduper,
    )
    pending_bytes = _write_jsonl_rows(
        unresolved_calls_path,
        (_direct_call_evidence_to_json(evidence) for evidence in result.unresolved_calls),
    )
    return _MapSegmentManifest(
        facts_path=facts_path,
        relatives_path=relatives_path,
        relatives_index_path=relatives_index_path,
        unresolved_calls_path=unresolved_calls_path,
        fact_count=len(result.facts),
        relative_count=relative_write.relative_count,
        unresolved_call_count=len(result.unresolved_calls),
        fact_kind_counts=dict(sorted(fact_kind_counts.items())),
        relation_kind_counts=relative_write.relation_kind_counts,
        conditional_relative_count=relative_write.conditional_relative_count,
        condition_kind_count=relative_write.condition_kind_count,
        relative_line_bytes=relative_write.relative_line_bytes,
        relative_index_bytes=relative_write.relative_index_bytes,
        byte_count=facts_bytes + relative_write.relative_line_bytes + relative_write.relative_index_bytes + pending_bytes,
        relative_map_input_count=relative_write.dedup_stats.relative_map_input_count,
        relative_map_written_count=relative_write.dedup_stats.relative_map_written_count,
        relative_map_skipped_exact_count=relative_write.dedup_stats.relative_map_skipped_exact_count,
        relative_worker_duplicate_exact_count=relative_write.dedup_stats.relative_worker_duplicate_exact_count,
        relative_worker_duplicate_conflict_count=relative_write.dedup_stats.relative_worker_duplicate_conflict_count,
        relative_worker_dedup_tracked_entry_count=(
            relative_write.dedup_stats.relative_worker_dedup_tracked_entry_count
        ),
        relative_worker_dedup_saturated_count=relative_write.dedup_stats.relative_worker_dedup_saturated_count,
    )


def _write_jsonl_rows(path: Path, rows: Iterable[Dict[str, JSONValue]]) -> int:
    byte_count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            line = _canonical_spool_json(row) + "\n"
            handle.write(line)
            byte_count += len(line.encode("utf-8"))
    return byte_count


def _write_encoded_fact_rows(path: Path, facts: Iterable[CodeFact]) -> int:
    byte_count = 0
    with path.open("w", encoding="utf-8") as handle:
        for fact in facts:
            line = _encoded_fact_from_code_fact(fact).read_line_text()
            handle.write(line)
            byte_count += len(line.encode("utf-8"))
    return byte_count


def _write_encoded_relative_rows(
    path: Path,
    index_path: Path,
    relatives: Iterable[FactRelative],
    *,
    relative_deduper: Optional[_WorkerRelativeDeduper] = None,
) -> _RelativeSegmentWriteResult:
    ordered = sorted(relatives, key=lambda relative: relative.relative_id)
    return _write_encoded_relative_line_rows(
        path,
        index_path,
        (_encoded_relative_from_fact_relative(relative) for relative in ordered),
        relative_deduper=relative_deduper,
    )


def _write_encoded_relative_line_rows(
    path: Path,
    index_path: Path,
    relatives: Iterable[EncodedRelativeLine],
    *,
    relative_deduper: Optional[_WorkerRelativeDeduper] = None,
) -> _RelativeSegmentWriteResult:
    byte_count = 0
    index_byte_count = 0
    relative_count = 0
    relation_kind_counts: Counter[str] = Counter()
    conditional_relative_count = 0
    condition_kinds: Set[str] = set()
    before_stats = relative_deduper.snapshot() if relative_deduper is not None else _WorkerRelativeDedupStats()
    local_input_count = 0
    local_written_count = 0
    with path.open("w", encoding="utf-8") as handle, index_path.open("w", encoding="utf-8") as index_handle:
        for relative in relatives:
            if relative_deduper is not None:
                if not relative_deduper.should_write(relative):
                    continue
            else:
                local_input_count += 1
                local_written_count += 1
            line = relative.read_line_text()
            handle.write(line)
            line_bytes = len(line.encode("utf-8"))
            byte_count += line_bytes
            relative_count += 1
            relation_kind_counts[relative.relation_kind] += 1
            if relative.condition is not None:
                conditional_relative_count += 1
                condition_kind = relative.condition.get("kind") if isinstance(relative.condition, dict) else None
                if isinstance(condition_kind, str):
                    condition_kinds.add(condition_kind)
            index_line = _relative_index_entry_to_text(
                _RelativeIndexEntry(
                    relative_id=relative.relative_id,
                    from_fact_id=relative.from_fact_id,
                    to_fact_id=relative.to_fact_id,
                    relation_kind=relative.relation_kind,
                    object_profile=relative.object_profile,
                    condition_json=_canonical_spool_json(relative.condition) if relative.condition is not None else None,
                    line_byte_count=line_bytes,
                    line_sha256=_hash_text(line),
                )
            )
            index_handle.write(index_line)
            index_byte_count += len(index_line.encode("utf-8"))
    if relative_deduper is not None:
        dedup_stats = _worker_relative_dedup_stats_delta(before_stats, relative_deduper.snapshot())
    else:
        dedup_stats = _WorkerRelativeDedupStats(
            relative_map_input_count=local_input_count,
            relative_map_written_count=local_written_count,
        )
    return _RelativeSegmentWriteResult(
        relative_line_bytes=byte_count,
        relative_index_bytes=index_byte_count,
        relative_count=relative_count,
        relation_kind_counts=dict(sorted(relation_kind_counts.items())),
        conditional_relative_count=conditional_relative_count,
        condition_kind_count=len(condition_kinds),
        dedup_stats=dedup_stats,
    )


def _relative_index_entry_to_text(entry: _RelativeIndexEntry) -> str:
    fields = [
        _b64_field(entry.relative_id),
        _b64_field(entry.from_fact_id),
        _b64_field(entry.to_fact_id),
        _b64_field(entry.relation_kind),
        _b64_field(entry.object_profile),
        _b64_field(entry.condition_json or ""),
        str(entry.line_byte_count),
        entry.line_sha256,
    ]
    return "\t".join(fields) + "\n"


def _relative_index_entry_from_text(text: str, *, line_number: int) -> _RelativeIndexEntry:
    parts = text.rstrip("\n").split("\t")
    if len(parts) != 8:
        raise _make_init_error(
            "map_reduce_segment_malformed",
            "relative segment index row must have 8 fields",
            details={"line": line_number},
        )
    try:
        line_byte_count = int(parts[6])
    except ValueError as exc:
        raise _make_init_error(
            "map_reduce_segment_malformed",
            "relative segment index byte count must be an integer",
            details={"line": line_number},
        ) from exc
    if line_byte_count < 0 or not _is_sha256(parts[7]):
        raise _make_init_error(
            "map_reduce_segment_malformed",
            "relative segment index has invalid byte count or sha256",
            details={"line": line_number},
        )
    condition_json = _unb64_field(parts[5], line_number=line_number)
    if condition_json:
        try:
            condition_value = json.loads(condition_json)
        except json.JSONDecodeError as exc:
            raise _make_init_error(
                "map_reduce_segment_malformed",
                "relative segment index condition must contain valid JSON",
                details={"line": line_number},
            ) from exc
        if not isinstance(condition_value, dict):
            raise _make_init_error(
                "map_reduce_segment_malformed",
                "relative segment index condition must be a JSON object",
                details={"line": line_number},
            )
    return _RelativeIndexEntry(
        relative_id=_unb64_field(parts[0], line_number=line_number),
        from_fact_id=_unb64_field(parts[1], line_number=line_number),
        to_fact_id=_unb64_field(parts[2], line_number=line_number),
        relation_kind=_unb64_field(parts[3], line_number=line_number),
        object_profile=_unb64_field(parts[4], line_number=line_number),
        condition_json=condition_json if condition_json else None,
        line_byte_count=line_byte_count,
        line_sha256=parts[7],
    )


def _b64_field(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _unb64_field(value: str, *, line_number: int) -> str:
    try:
        return base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise _make_init_error(
            "map_reduce_segment_malformed",
            "relative segment index contains invalid escaped text",
            details={"line": line_number},
        ) from exc


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _iter_segment_facts(path: Path) -> Iterator[CodeFact]:
    for fact in _iter_segment_encoded_facts(path):
        yield _code_fact_from_encoded_fact_line(fact)


def _iter_segment_encoded_facts(path: Path) -> Iterator[EncodedFactLine]:
    for row, line_text in _iter_jsonl_rows_with_text(path):
        yield EncodedFactLine.from_stored_line(StoredFactLine.from_json(row), line_text=line_text)


def _iter_segment_relatives(path: Path) -> Iterator[FactRelative]:
    for relative in _iter_segment_encoded_relatives(path):
        yield _relative_from_encoded_relative_line(relative)


def _iter_segment_encoded_relatives(path: Path) -> Iterator[EncodedRelativeLine]:
    for row, line_text in _iter_jsonl_rows_with_text(path):
        yield EncodedRelativeLine.from_stored_line(StoredRelativeLine.from_json(row), line_text=line_text)


def _iter_segment_unresolved_calls(path: Path) -> Iterator[DirectCallEvidence]:
    for row in _iter_jsonl_rows(path):
        yield _direct_call_evidence_from_json(row)


def _relative_segment_from_map_manifest(manifest: _MapSegmentManifest) -> _RelativeSegmentManifest:
    return _RelativeSegmentManifest(
        relatives_path=manifest.relatives_path,
        relatives_index_path=manifest.relatives_index_path,
        relative_count=manifest.relative_count,
        relative_line_bytes=manifest.relative_line_bytes,
        relative_index_bytes=manifest.relative_index_bytes,
    )


def _relative_segment_from_resolved_manifest(manifest: _ResolvedRelativeSegmentManifest) -> _RelativeSegmentManifest:
    return _RelativeSegmentManifest(
        relatives_path=manifest.relatives_path,
        relatives_index_path=manifest.relatives_index_path,
        relative_count=manifest.relative_count,
        relative_line_bytes=manifest.relative_line_bytes,
        relative_index_bytes=manifest.relative_index_bytes,
    )


def _iter_relative_segment_indexed_lines(manifest: _RelativeSegmentManifest) -> Iterator[_IndexedRelativeLine]:
    previous_id: Optional[str] = None
    data_count = 0
    with manifest.relatives_index_path.open("r", encoding="utf-8") as index_handle, manifest.relatives_path.open(
        "r",
        encoding="utf-8",
    ) as data_handle:
        for line_number, index_line in enumerate(index_handle, start=1):
            if not index_line.strip():
                continue
            data_line = data_handle.readline()
            if data_line == "":
                raise _make_init_error(
                    "map_reduce_segment_malformed",
                    "relative segment index has more rows than data",
                    details={"line": line_number},
                )
            entry = _relative_index_entry_from_text(index_line, line_number=line_number)
            if previous_id is not None and entry.relative_id < previous_id:
                raise _make_init_error(
                    "map_reduce_segment_malformed",
                    "relative segment index must be sorted by relative_id",
                    details={"line": line_number, "relative_id": entry.relative_id},
                )
            previous_id = entry.relative_id
            if len(data_line.encode("utf-8")) != entry.line_byte_count or _hash_text(data_line) != entry.line_sha256:
                raise _make_init_error(
                    "map_reduce_segment_malformed",
                    "relative segment index does not match data line",
                    details={"line": line_number, "relative_id": entry.relative_id},
                )
            data_count += 1
            yield _IndexedRelativeLine(entry=entry, line_text=data_line)
        if data_handle.readline() != "":
            raise _make_init_error(
                "map_reduce_segment_malformed",
                "relative segment data has more rows than index",
            )
    if data_count != manifest.relative_count:
        raise _make_init_error(
            "map_reduce_segment_malformed",
            "relative segment row count differs from manifest",
            details={"expected": manifest.relative_count, "actual": data_count},
        )


def _iter_external_merged_relative_segments(
    manifests: Sequence[Union[_MapSegmentManifest, _ResolvedRelativeSegmentManifest, _RelativeSegmentManifest]],
    stats: _RelativeExternalMergeStats,
    *,
    fan_in: Optional[int] = None,
) -> Iterator[EncodedRelativeLine]:
    started = time.perf_counter()
    relative_segments = [_normalize_relative_segment_manifest(manifest) for manifest in manifests if _manifest_relative_count(manifest) > 0]
    stats.segment_count = len(relative_segments)
    stats.input_bytes = sum(manifest.relative_line_bytes for manifest in relative_segments)
    stats.index_bytes = sum(manifest.relative_index_bytes for manifest in relative_segments)
    stats.fan_in = _relative_merge_fan_in(len(relative_segments), requested_fan_in=fan_in)
    try:
        if not relative_segments:
            return
        if len(relative_segments) <= stats.fan_in:
            stats.pass_count += 1
            yield from _iter_external_merged_relative_segment_batch(
                relative_segments,
                stats,
                count_input=True,
                count_accepted=True,
            )
            return
        temp_parent = _relative_merge_temp_parent(relative_segments)
        with tempfile.TemporaryDirectory(prefix="relative-merge-", dir=temp_parent) as temp_name:
            current_segments = relative_segments
            pass_index = 0
            count_input = True
            while len(current_segments) > stats.fan_in:
                pass_index += 1
                stats.pass_count += 1
                next_segments: List[_RelativeSegmentManifest] = []
                pass_dir = Path(temp_name) / f"pass-{pass_index:04d}"
                for chunk_index, chunk in enumerate(_chunks(current_segments, stats.fan_in)):
                    run_dir = pass_dir / f"run-{chunk_index:04d}"
                    merged = _iter_external_merged_relative_segment_batch(
                        chunk,
                        stats,
                        count_input=count_input,
                        count_accepted=False,
                    )
                    next_segments.append(_write_relative_merge_run(run_dir, merged))
                current_segments = next_segments
                count_input = False
            stats.pass_count += 1
            yield from _iter_external_merged_relative_segment_batch(
                current_segments,
                stats,
                count_input=False,
                count_accepted=True,
            )
    finally:
        stats.duration_ms = round(_elapsed_ms(started), 3)


def _relative_merge_fan_in(segment_count: int, *, requested_fan_in: Optional[int] = None) -> int:
    if segment_count <= 0:
        return 0
    if segment_count == 1:
        return 1
    fan_in = int(requested_fan_in) if requested_fan_in is not None else RELATIVE_MERGE_DEFAULT_FAN_IN
    if requested_fan_in is None and resource is not None:
        try:
            soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        except (OSError, ValueError):
            soft_limit = 0
        if soft_limit and soft_limit > 0 and soft_limit != resource.RLIM_INFINITY:
            fd_budget = max(
                RELATIVE_MERGE_MIN_FAN_IN,
                (int(soft_limit) - RELATIVE_MERGE_FD_HEADROOM) // RELATIVE_MERGE_FD_PER_SEGMENT,
            )
            fan_in = min(fan_in, fd_budget)
    return min(segment_count, max(RELATIVE_MERGE_MIN_FAN_IN, fan_in))


def _iter_external_merged_relative_segment_batch(
    relative_segments: Sequence[_RelativeSegmentManifest],
    stats: _RelativeExternalMergeStats,
    *,
    count_input: bool,
    count_accepted: bool,
) -> Iterator[EncodedRelativeLine]:
    heap: List[Tuple[str, int, int, _IndexedRelativeLine, Iterator[_IndexedRelativeLine]]] = []
    active_iterators: List[Iterator[_IndexedRelativeLine]] = []
    active_segment_count = 0
    sequence = 0
    try:
        for segment_order, manifest in enumerate(relative_segments):
            iterator = _iter_relative_segment_indexed_lines(manifest)
            try:
                item = next(iterator)
            except StopIteration:
                continue
            active_iterators.append(iterator)
            active_segment_count += 1
            if count_input:
                stats.input_count += 1
            heapq.heappush(heap, (item.entry.relative_id, segment_order, sequence, item, iterator))
            sequence += 1
        stats.max_heap_size = max(stats.max_heap_size, len(heap))
        stats.peak_open_segment_count = max(stats.peak_open_segment_count, active_segment_count)
        while heap:
            relative_id = heap[0][0]
            accepted: Optional[_IndexedRelativeLine] = None
            while heap and heap[0][0] == relative_id:
                _current_id, segment_order, _item_order, item, iterator = heapq.heappop(heap)
                if accepted is None:
                    accepted = item
                elif item.line_text == accepted.line_text:
                    stats.duplicate_exact_count += 1
                else:
                    stats.conflict_count += 1
                    raise _make_init_error(
                        "map_reduce_conflict",
                        "duplicate relative id has non-idempotent payload",
                        details={"relative_id": relative_id},
                    )
                try:
                    next_item = next(iterator)
                except StopIteration:
                    active_segment_count -= 1
                    continue
                if count_input:
                    stats.input_count += 1
                heapq.heappush(heap, (next_item.entry.relative_id, segment_order, sequence, next_item, iterator))
                sequence += 1
                stats.max_heap_size = max(stats.max_heap_size, len(heap))
                stats.peak_open_segment_count = max(stats.peak_open_segment_count, active_segment_count)
            if accepted is not None:
                if count_accepted:
                    stats.accepted_count += 1
                yield accepted.to_encoded_relative_line()
    finally:
        for iterator in active_iterators:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()


def _write_relative_merge_run(
    segment_dir: Path,
    relatives: Iterable[EncodedRelativeLine],
) -> _RelativeSegmentManifest:
    segment_dir.mkdir(parents=True, exist_ok=False)
    relatives_path = segment_dir / "relatives.jsonl"
    relatives_index_path = segment_dir / "relatives.index"
    write = _write_encoded_relative_line_rows(relatives_path, relatives_index_path, relatives)
    return _RelativeSegmentManifest(
        relatives_path=relatives_path,
        relatives_index_path=relatives_index_path,
        relative_count=write.relative_count,
        relative_line_bytes=write.relative_line_bytes,
        relative_index_bytes=write.relative_index_bytes,
    )


def _relative_merge_temp_parent(relative_segments: Sequence[_RelativeSegmentManifest]) -> Optional[str]:
    if not relative_segments:
        return None
    parent = relative_segments[0].relatives_path.parent
    run_parent = parent.parent
    if run_parent.exists():
        return str(run_parent)
    return str(parent) if parent.exists() else None


def _chunks(values: Sequence[_RelativeSegmentManifest], size: int) -> Iterator[Sequence[_RelativeSegmentManifest]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _normalize_relative_segment_manifest(
    manifest: Union[_MapSegmentManifest, _ResolvedRelativeSegmentManifest, _RelativeSegmentManifest],
) -> _RelativeSegmentManifest:
    if isinstance(manifest, _RelativeSegmentManifest):
        return manifest
    if isinstance(manifest, _MapSegmentManifest):
        return _relative_segment_from_map_manifest(manifest)
    return _relative_segment_from_resolved_manifest(manifest)


def _manifest_relative_count(
    manifest: Union[_MapSegmentManifest, _ResolvedRelativeSegmentManifest, _RelativeSegmentManifest],
) -> int:
    return manifest.relative_count


def _iter_jsonl_rows(path: Path) -> Iterator[Dict[str, JSONValue]]:
    for row, _line_text in _iter_jsonl_rows_with_text(path):
        yield row


def _iter_jsonl_rows_with_text(path: Path) -> Iterator[Tuple[Dict[str, JSONValue], str]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise _make_init_error(
                    "map_reduce_segment_malformed",
                    "map-reduce segment must contain valid JSONL",
                    details={"line": line_number},
                ) from exc
            if not isinstance(row, dict):
                raise _make_init_error(
                    "map_reduce_segment_malformed",
                    "map-reduce segment row must be a JSON object",
                    details={"line": line_number},
                )
            yield row, stripped + "\n"


class _StreamingExtraction:
    def __init__(
        self,
        extractor: CodeFactExtractor,
        source_roots: Optional[Sequence[Union[str, Path]]],
        profile: str,
    ) -> None:
        self.extractor = extractor
        self.source_roots = source_roots
        self.profile = profile
        self.source_count = 0
        self.errors: List[Exception] = []
        self._sources: List[Path] = []
        self._successful_sources: List[Path] = []
        self._compile_lookup_by_source: Dict[Path, _CompileCommandLookup] = {}
        self._worker_header_cache_entry_counts: Dict[int, int] = {}
        self._effective_worker_count = 1
        self._facts_by_kind: Counter[str] = Counter()
        self._relatives_by_kind: Counter[str] = Counter()
        self._fact_count = 0
        self._relative_count = 0
        self._unresolved_call_count = 0
        self._direct_call_index = _DirectCallResolutionIndex(
            functions_by_id={},
            functions_by_name={},
            functions_by_source_name={},
        )
        self._header_cache = _HeaderMaterializationCache()
        self._relative_deduper = _WorkerRelativeDeduper()
        self._header_stats = _HeaderMaterializationStats()
        self._staging_dir: Optional[Path] = None
        self._run_id: Optional[str] = None
        self._stale_run_gc_count = 0
        self._map_segment_count = 0
        self._map_segment_bytes = 0
        self._resolved_segment_count = 0
        self._resolved_segment_bytes = 0
        self._fact_duplicate_exact_count = 0
        self._fact_duplicate_merge_parse_count = 0
        self._fact_duplicate_conflict_count = 0
        self._fact_line_reencoded_count = 0
        self._relative_duplicate_exact_count = 0
        self._relative_duplicate_conflict_count = 0
        self._relative_line_reencoded_count = 0
        self._relative_segment_manifests: List[_RelativeSegmentManifest] = []
        self._relative_map_input_count = 0
        self._relative_map_written_count = 0
        self._relative_map_skipped_exact_count = 0
        self._relative_worker_duplicate_exact_count = 0
        self._relative_worker_duplicate_conflict_count = 0
        self._relative_worker_dedup_tracked_counts: Dict[int, int] = {}
        self._relative_worker_dedup_saturated_counts: Dict[int, int] = {}
        self._db_path: Optional[Path] = None
        self._connection: Optional[sqlite3.Connection] = None
        self._facts_started = False
        self._facts_finished = False
        self._relatives_started = False
        self._relatives_finished = False
        self._source_inventory_started = False

    def __enter__(self) -> "_StreamingExtraction":
        self._staging_dir = self._open_map_reduce_staging()
        db_path = self._staging_dir / "reduce.sqlite"
        self._db_path = db_path
        connection = sqlite3.connect(str(db_path))
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(
            """
            CREATE TABLE facts (
                object_id TEXT PRIMARY KEY,
                source_seq INTEGER NOT NULL,
                fact_kind TEXT NOT NULL,
                linkage TEXT,
                canonical_source TEXT,
                object_name TEXT NOT NULL,
                object_source TEXT NOT NULL,
                object_profile TEXT NOT NULL,
                object_caller TEXT,
                object_callee TEXT,
                line TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )
        connection.execute(
            """
            CREATE TABLE relatives (
                relative_id TEXT PRIMARY KEY,
                from_fact_id TEXT NOT NULL,
                to_fact_id TEXT NOT NULL,
                relation_kind TEXT NOT NULL,
                condition_json TEXT,
                object_profile TEXT NOT NULL,
                line TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )
        connection.execute("CREATE TABLE unresolved_calls (seq INTEGER PRIMARY KEY AUTOINCREMENT, line TEXT NOT NULL)")
        self._connection = connection
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._staging_dir is not None:
            lock_path = self._staging_dir / ".lock"
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            shutil.rmtree(self._staging_dir, ignore_errors=True)
            self._staging_dir = None
            self._run_id = None
            self._db_path = None

    @property
    def facts(self) -> Iterator[CodeFact]:
        return self._iter_facts()

    @property
    def encoded_facts(self) -> Iterator[EncodedFactLine]:
        return self._iter_encoded_facts()

    @property
    def relatives(self) -> Iterator[FactRelative]:
        return self._iter_relatives()

    @property
    def encoded_relatives(self) -> Iterator[EncodedRelativeLine]:
        return self._iter_encoded_relatives()

    @property
    def source_inventory(self) -> Iterator[SourceInventoryEntry]:
        return self._iter_source_inventory()

    @property
    def unresolved_calls(self) -> List[DirectCallEvidence]:
        if not self._facts_finished:
            raise RuntimeError("streaming extraction facts must be consumed before unresolved calls are read")
        return list(self._iter_spooled_unresolved_calls())

    @property
    def fact_count(self) -> int:
        return self._fact_count

    @property
    def relative_count(self) -> int:
        return self._relative_count

    @property
    def facts_by_kind(self) -> Dict[str, int]:
        return dict(sorted(self._facts_by_kind.items()))

    @property
    def relatives_by_kind(self) -> Dict[str, int]:
        return dict(sorted(self._relatives_by_kind.items()))

    def _open_map_reduce_staging(self) -> Path:
        run_root = self.extractor.target_repo / ".arbiter" / "facts" / "run" / "initializer-mapreduce"
        run_root.mkdir(parents=True, exist_ok=True)
        self._stale_run_gc_count = _gc_stale_map_reduce_runs(run_root)
        run_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
        staging_dir = run_root / run_id
        staging_dir.mkdir(parents=False, exist_ok=False)
        self._run_id = run_id
        lock_payload = {
            "run_id": run_id,
            "pid": os.getpid(),
            "created_at_unix": round(time.time(), 6),
        }
        (staging_dir / ".lock").write_text(
            _canonical_spool_json(lock_payload) + "\n",
            encoding="utf-8",
        )
        (staging_dir / "map").mkdir()
        (staging_dir / "resolved").mkdir()
        return staging_dir

    def _source_segment_dir(self, seq: int) -> Path:
        if self._staging_dir is None:
            raise RuntimeError("streaming extraction staging is not open")
        return self._staging_dir / "map" / f"{seq:08d}"

    def _resolved_segment_dir(self) -> Path:
        if self._staging_dir is None:
            raise RuntimeError("streaming extraction staging is not open")
        return self._staging_dir / "resolved"

    def _iter_facts(self) -> Iterator[CodeFact]:
        for fact in self._iter_encoded_facts():
            yield _code_fact_from_encoded_fact_line(fact)

    def _iter_encoded_facts(self) -> Iterator[EncodedFactLine]:
        if self._facts_started:
            raise RuntimeError("streaming extraction facts can only be consumed once")
        self._facts_started = True
        try:
            started = time.perf_counter()
            extraction_started = started
            self.extractor.compile_command_index = self.extractor._load_compile_command_index()
            self._sources = self.extractor._collect_source_files(self.source_roots)
            self.source_count = len(self._sources)
            self.extractor._emit_progress_event(
                "sources_planned",
                started=started,
                total=self.source_count,
                payload={
                    "profile": self.profile,
                    "compile_database_configured": self.extractor.compile_command_index is not None,
                },
            )
            if self._sources:
                self.extractor._validate_toolchain()
            active_worker_count = min(
                max(1, self.extractor.config.extractor_worker_count),
                max(1, self.source_count),
            )
            self._effective_worker_count = active_worker_count
            if self._sources:
                if active_worker_count <= 1:
                    for seq, source in enumerate(self._sources):
                        self._extract_source_facts(seq, source)
                else:
                    self._iter_parallel_source_facts(active_worker_count)
            self.extractor._emit_worker_pool_event(
                source_count=self.source_count,
                worker_count=self._effective_worker_count,
                max_unmerged=self._effective_worker_count,
                successful_file_count=len(self._successful_sources),
                skipped_file_count=self.source_count - len(self._successful_sources),
                partial_ast_count=sum(1 for error in self.errors if getattr(error, "code", None) == "clang_ast_partial"),
                warning_count=len(self.errors),
                profile=self.profile,
                started=extraction_started,
                header_decl_cache_entry_count=self._header_cache_entry_count(),
                map_output_segment_count=self._map_segment_count,
                map_output_bytes=self._map_segment_bytes,
                stale_run_gc_count=self._stale_run_gc_count,
                relative_map_input_count=self._relative_map_input_count,
                relative_map_written_count=self._relative_map_written_count,
                relative_map_skipped_exact_count=self._relative_map_skipped_exact_count,
                relative_worker_duplicate_exact_count=self._relative_worker_duplicate_exact_count,
                relative_worker_duplicate_conflict_count=self._relative_worker_duplicate_conflict_count,
                relative_worker_dedup_tracked_entry_count=sum(self._relative_worker_dedup_tracked_counts.values()),
                relative_worker_dedup_saturated_count=sum(self._relative_worker_dedup_saturated_counts.values()),
                fact_line_passthrough_count=self._fact_count,
                relative_line_passthrough_count=self._relative_segment_input_count(),
                fact_line_passthrough_bytes=self._spooled_line_bytes("facts"),
                relative_line_passthrough_bytes=self._relative_segment_input_bytes(),
                fact_line_reencoded_count=self._fact_line_reencoded_count,
                relative_line_reencoded_count=self._relative_line_reencoded_count,
                fact_duplicate_exact_count=self._fact_duplicate_exact_count,
                fact_duplicate_merge_parse_count=self._fact_duplicate_merge_parse_count,
                fact_duplicate_conflict_count=self._fact_duplicate_conflict_count,
                relative_duplicate_exact_count=self._relative_duplicate_exact_count,
                relative_duplicate_conflict_count=self._relative_duplicate_conflict_count,
            )
            self._commit()
        except Exception:
            # Only a BUILD failure (before the spool was committed) leaves facts unfinished.
            # Exceptions raised by the consumer during the yield phase below must NOT reset
            # this flag — the spool is already built, and resetting it would corrupt the
            # consume-once guards for relatives/source_inventory.
            self._facts_finished = False
            raise
        self._facts_finished = True
        yield from self._iter_spooled_encoded_facts()

    def _extract_source_facts(self, seq: int, source: Path) -> None:
        outcome = self._run_file_work_item(self._make_work_item(seq, source))
        self._merge_file_outcome(outcome)

    def _iter_parallel_source_facts(self, worker_count: int) -> None:
        next_submit = 0
        futures = {}
        backend_spec = _process_worker_backend_spec(self.extractor)
        try:
            executor = ProcessPoolExecutor(
                max_workers=worker_count,
                initializer=_initialize_process_file_worker,
                initargs=(self.extractor.target_repo, self.extractor.config, backend_spec),
            )
        except OSError:
            self._effective_worker_count = 1
            for seq, source in enumerate(self._sources):
                self._extract_source_facts(seq, source)
            return
        merged_sources: set = set()
        broke = False
        with executor:
            def submit_available() -> None:
                nonlocal next_submit
                while next_submit < len(self._sources) and len(futures) < worker_count:
                    item = self._make_work_item(next_submit, self._sources[next_submit])
                    futures[executor.submit(_run_file_work_item_in_process, item)] = item
                    next_submit += 1

            submit_available()
            try:
                while futures:
                    done, _pending = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        item = futures.pop(future)
                        self._merge_file_outcome(future.result())
                        merged_sources.add(item.source)
                        submit_available()
            except BrokenProcessPool:
                # A worker process died abruptly — almost always OOM-killed parsing one heavy
                # TU at this parallelism, which poisons the whole pool. Extraction is stateless
                # per file, so nothing is lost but the one in-flight TU: finish the rest SERIALLY
                # (one TU at a time bounds memory), best-effort, rather than aborting the index.
                broke = True
                for future in futures:
                    future.cancel()
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        if broke:
            for seq, source in enumerate(self._sources):
                if source in merged_sources:
                    continue
                try:
                    self._extract_source_facts(seq, source)
                except Exception:
                    continue

    def _make_work_item(self, seq: int, source: Path) -> _FileWorkItem:
        rel_source = _relative_source(self.extractor.target_repo, source)
        compile_lookup = self._compile_lookup_by_source.get(source)
        if compile_lookup is None:
            compile_lookup = self.extractor._lookup_compile_command(source)
            self._compile_lookup_by_source[source] = compile_lookup
        return _FileWorkItem(
            seq=seq,
            source=source,
            rel_source=rel_source,
            profile=self.profile,
            source_id=_source_id(rel_source, self.profile),
            compile_lookup=compile_lookup,
            segment_dir=self._source_segment_dir(seq),
        )

    def _run_file_work_item(self, item: _FileWorkItem) -> _FileWorkOutcome:
        return _run_file_work_item_with_cache(
            self.extractor,
            self._header_cache,
            item,
            relative_deduper=self._relative_deduper,
        )

    def _merge_file_outcome(self, outcome: _FileWorkOutcome) -> None:
        if outcome.worker_id is not None and outcome.worker_header_cache_entry_count is not None:
            self._worker_header_cache_entry_counts[outcome.worker_id] = outcome.worker_header_cache_entry_count
        if outcome.error_code is not None:
            details = _diagnostic_details(
                outcome.diagnostic_kind,
                outcome.diagnostic_reason,
                outcome.diagnostic_details,
            )
            error = _make_init_error(
                outcome.error_code,
                outcome.error_message or outcome.error_code,
                source=outcome.rel_source,
                details=details,
            )
            self.errors.append(error)
            if outcome.error_code == "clang_ast_failed":
                self.extractor._emit_file_warning(
                    outcome.rel_source,
                    outcome.error_code,
                    outcome.profile,
                    outcome.started,
                    outcome.diagnostic_kind,
                    outcome.compile_lookup,
                    diagnostic_reason=outcome.diagnostic_reason,
                    diagnostic_details=outcome.diagnostic_details,
                )
                return
            self.extractor._emit_file_error(outcome.rel_source, outcome.error_code, outcome.profile, outcome.started)
            return

        file_result = outcome.file_result
        if file_result is None:
            raise RuntimeError("file work outcome missing file result")
        manifest = outcome.segment_manifest
        if manifest is None:
            self._merge_file_result_records(file_result, outcome.seq)
        else:
            self._merge_file_segment_records(manifest, outcome.seq, worker_id=outcome.worker_id)
        self._successful_sources.append(outcome.source)
        self._record_header_stats(file_result.stats)
        if outcome.worker_id is None:
            self._header_cache.publish(
                producer_seq=outcome.seq,
                context_hash=file_result.header_context_hash,
                keys=file_result.header_decl_keys,
                seed=file_result.header_resolver_seed,
            )
        if file_result.warning_code is not None:
            self.errors.append(
                _make_init_error(
                    file_result.warning_code,
                    "clang AST invocation produced partial output",
                    source=outcome.rel_source,
                    details=_diagnostic_details(
                        file_result.ast_diagnostic_kind,
                        file_result.ast_diagnostic_reason,
                    ),
                )
            )
        self.extractor._emit_file_event(
            outcome.rel_source,
            file_result.facts,
            file_result.relatives,
            file_result.stats,
            outcome.profile,
            outcome.started,
            outcome.compile_lookup,
            file_result.ast_diagnostic_reason,
            backend=file_result.backend,
            parse_duration_ms=file_result.parse_duration_ms,
            traverse_duration_ms=file_result.traverse_duration_ms,
            fact_count=manifest.fact_count if manifest is not None else None,
            relative_count=manifest.relative_count if manifest is not None else None,
            conditional_relative_count=manifest.conditional_relative_count if manifest is not None else None,
            fact_kind_counts=manifest.fact_kind_counts if manifest is not None else None,
            relation_kind_counts=manifest.relation_kind_counts if manifest is not None else None,
            condition_kind_count=manifest.condition_kind_count if manifest is not None else None,
            relative_map_input_count=manifest.relative_map_input_count if manifest is not None else None,
            relative_map_written_count=manifest.relative_map_written_count if manifest is not None else None,
            relative_map_skipped_exact_count=manifest.relative_map_skipped_exact_count if manifest is not None else None,
        )
        if self._fact_count % STREAMING_SPOOL_COMMIT_INTERVAL == 0:
            self._commit()

    def _merge_file_result_records(self, file_result: _FileMapResult, source_seq: int) -> None:
        for fact in file_result.facts:
            encoded = _encoded_fact_from_code_fact(fact)
            if not self._spool_encoded_fact(encoded, source_seq):
                continue
            self._fact_count += 1
            self._facts_by_kind[encoded.fact_kind] += 1
            self._index_function_fact(fact)
        for relative in file_result.relatives:
            self._spool_encoded_relative(_encoded_relative_from_fact_relative(relative))
        for evidence in file_result.unresolved_calls:
            self._spool_unresolved_call(evidence)

    def _merge_file_segment_records(
        self,
        manifest: _MapSegmentManifest,
        source_seq: int,
        *,
        worker_id: Optional[int] = None,
    ) -> None:
        self._map_segment_count += 3
        self._map_segment_bytes += manifest.byte_count
        self._relative_map_input_count += manifest.relative_map_input_count
        self._relative_map_written_count += manifest.relative_map_written_count
        self._relative_map_skipped_exact_count += manifest.relative_map_skipped_exact_count
        self._relative_worker_duplicate_exact_count += manifest.relative_worker_duplicate_exact_count
        self._relative_worker_duplicate_conflict_count += manifest.relative_worker_duplicate_conflict_count
        relative_worker_key = worker_id if worker_id is not None else 0
        self._relative_worker_dedup_tracked_counts[relative_worker_key] = max(
            self._relative_worker_dedup_tracked_counts.get(relative_worker_key, 0),
            manifest.relative_worker_dedup_tracked_entry_count,
        )
        self._relative_worker_dedup_saturated_counts[relative_worker_key] = max(
            self._relative_worker_dedup_saturated_counts.get(relative_worker_key, 0),
            manifest.relative_worker_dedup_saturated_count,
        )
        for fact in _iter_segment_encoded_facts(manifest.facts_path):
            if not self._spool_encoded_fact(fact, source_seq):
                continue
            self._fact_count += 1
            self._facts_by_kind[fact.fact_kind] += 1
        if manifest.relative_count:
            self._relative_segment_manifests.append(_relative_segment_from_map_manifest(manifest))
        for evidence in _iter_segment_unresolved_calls(manifest.unresolved_calls_path):
            self._spool_unresolved_call(evidence)

    def _record_header_stats(self, stats: _FileMapStats) -> None:
        self._header_stats.header_decl_cache_entry_count = max(
            self._header_stats.header_decl_cache_entry_count,
            stats.header_decl_cache_entry_count,
        )
        self._header_stats.header_decl_cache_hit_count += stats.header_decl_cache_hit_count
        self._header_stats.header_decl_cache_miss_count += stats.header_decl_cache_miss_count
        self._header_stats.header_decl_skipped_subtree_count += stats.header_decl_skipped_subtree_count
        self._header_stats.header_decl_seed_count += stats.header_decl_seed_count

    def _header_cache_entry_count(self) -> int:
        if self._worker_header_cache_entry_counts:
            return sum(self._worker_header_cache_entry_counts.values())
        return self._header_cache.entry_count()

    def _spooled_line_bytes(self, table_name: str) -> int:
        if table_name not in {"facts", "relatives"}:
            raise RuntimeError("unknown reducer table")
        row = self._require_connection().execute(
            f"SELECT COALESCE(SUM(length(CAST(line AS BLOB))), 0) FROM {table_name}"
        ).fetchone()
        return int(row[0] or 0)

    def _relative_segment_input_count(self) -> int:
        if self._relative_segment_manifests:
            return sum(manifest.relative_count for manifest in self._relative_segment_manifests)
        return self._relative_count

    def _relative_segment_input_bytes(self) -> int:
        if self._relative_segment_manifests:
            return sum(manifest.relative_line_bytes for manifest in self._relative_segment_manifests)
        return self._spooled_line_bytes("relatives")

    def _iter_relatives(self) -> Iterator[FactRelative]:
        for relative in self._iter_encoded_relatives():
            yield _relative_from_encoded_relative_line(relative)

    def _iter_encoded_relatives(self) -> Iterator[EncodedRelativeLine]:
        if not self._facts_finished:
            raise RuntimeError("streaming extraction facts must be consumed before relatives")
        if self._relatives_started:
            raise RuntimeError("streaming extraction relatives can only be consumed once")
        self._relatives_started = True
        try:
            self._resolve_and_spool_direct_calls()
            self._commit()
        except Exception:
            # Only a BUILD failure (before the spool was committed) leaves relatives
            # unfinished. Exceptions raised by the consumer during the yield phase below
            # must NOT reset this flag, so downstream consume-once state stays consistent.
            self._relatives_finished = False
            raise
        self._relatives_finished = True
        if self._relative_segment_manifests:
            stats = _RelativeExternalMergeStats()
            try:
                for relative in _iter_external_merged_relative_segments(self._relative_segment_manifests, stats):
                    self._record_encoded_relative(relative)
                    yield relative
            finally:
                self._relative_duplicate_exact_count += stats.duplicate_exact_count
                self._relative_duplicate_conflict_count += stats.conflict_count
                self.extractor._emit_relative_merge_event(stats, self.profile)
        else:
            yield from self._iter_spooled_encoded_relatives()

    def _iter_source_inventory(self) -> Iterator[SourceInventoryEntry]:
        if not self._facts_finished:
            raise RuntimeError("streaming extraction facts must be consumed before source inventory")
        if self._source_inventory_started:
            raise RuntimeError("streaming extraction source inventory can only be consumed once")
        self._source_inventory_started = True
        successful_sources = sorted(
            self._successful_sources,
            key=lambda source: _relative_source(self.extractor.target_repo, source),
        )
        inventory_sources = (
            self.extractor._collect_inventory_source_files(successful_sources)
            if self.extractor.compile_command_index is not None
            else successful_sources
        )
        entries = self.extractor._build_source_inventory(
            inventory_sources,
            self.profile,
            self._compile_lookup_by_source,
        )
        yield from sorted(entries, key=lambda entry: entry.source_id)

    def _spool_fact(self, fact: CodeFact, source_seq: int) -> bool:
        return self._spool_encoded_fact(_encoded_fact_from_code_fact(fact), source_seq)

    def _spool_encoded_fact(self, fact: EncodedFactLine, source_seq: int) -> bool:
        connection = self._require_connection()
        line_text = fact.read_line_text()
        try:
            connection.execute(
                """
                INSERT INTO facts(
                    object_id, source_seq, fact_kind, linkage, canonical_source,
                    object_name, object_source, object_profile, object_caller,
                    object_callee, line
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact.object_id,
                    source_seq,
                    fact.fact_kind,
                    fact.linkage,
                    fact.canonical_source,
                    fact.object_name,
                    fact.object_source,
                    fact.object_profile,
                    fact.object_caller,
                    fact.object_callee,
                    line_text,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            existing = connection.execute(
                "SELECT source_seq, line FROM facts WHERE object_id = ?",
                (fact.object_id,),
            ).fetchone()
            if existing is not None and source_seq < existing[0]:
                connection.execute(
                    """
                    UPDATE facts
                    SET source_seq = ?, fact_kind = ?, linkage = ?, canonical_source = ?,
                        object_name = ?, object_source = ?, object_profile = ?,
                        object_caller = ?, object_callee = ?, line = ?
                    WHERE object_id = ?
                    """,
                    (
                        source_seq,
                        fact.fact_kind,
                        fact.linkage,
                        fact.canonical_source,
                        fact.object_name,
                        fact.object_source,
                        fact.object_profile,
                        fact.object_caller,
                        fact.object_callee,
                        line_text,
                        fact.object_id,
                    ),
                )
                return False
            if existing is not None and source_seq > existing[0]:
                return False
            if existing is not None and existing[1] == line_text:
                self._fact_duplicate_exact_count += 1
                return False
            if existing is not None:
                self._fact_duplicate_merge_parse_count += 1
                merged = _merge_duplicate_encoded_fact_lines(existing[1], line_text)
                if merged is not None:
                    merged_line = merged.read_line_text()
                    if merged_line != existing[1]:
                        self._fact_line_reencoded_count += 1
                        connection.execute(
                            """
                            UPDATE facts
                            SET fact_kind = ?, linkage = ?, canonical_source = ?,
                                object_name = ?, object_source = ?, object_profile = ?,
                                object_caller = ?, object_callee = ?, line = ?
                            WHERE object_id = ?
                            """,
                            (
                                merged.fact_kind,
                                merged.linkage,
                                merged.canonical_source,
                                merged.object_name,
                                merged.object_source,
                                merged.object_profile,
                                merged.object_caller,
                                merged.object_callee,
                                merged_line,
                                fact.object_id,
                            ),
                        )
                    return False
            self._fact_duplicate_conflict_count += 1
            raise _make_init_error(
                "map_reduce_conflict",
                "duplicate fact id has non-idempotent payload",
                details={"object_id": fact.object_id},
            )

    def _spool_relative(self, relative: FactRelative) -> bool:
        return self._spool_encoded_relative(_encoded_relative_from_fact_relative(relative))

    def _spool_encoded_relative(self, relative: EncodedRelativeLine) -> bool:
        line_text = relative.read_line_text()
        connection = self._require_connection()
        try:
            connection.execute(
                """
                INSERT INTO relatives(
                    relative_id, from_fact_id, to_fact_id, relation_kind,
                    condition_json, object_profile, line
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relative.relative_id,
                    relative.from_fact_id,
                    relative.to_fact_id,
                    relative.relation_kind,
                    _canonical_spool_json(relative.condition) if relative.condition is not None else None,
                    relative.object_profile,
                    line_text,
                ),
            )
        except sqlite3.IntegrityError:
            existing = connection.execute(
                "SELECT line FROM relatives WHERE relative_id = ?",
                (relative.relative_id,),
            ).fetchone()
            if existing is not None and existing[0] == line_text:
                self._relative_duplicate_exact_count += 1
                return False
            self._relative_duplicate_conflict_count += 1
            raise _make_init_error(
                "map_reduce_conflict",
                "duplicate relative id has non-idempotent payload",
                details={"relative_id": relative.relative_id},
            )
        self._record_encoded_relative(relative)
        return True

    def _iter_spooled_facts(self) -> Iterator[CodeFact]:
        for fact in self._iter_spooled_encoded_facts():
            yield _code_fact_from_encoded_fact_line(fact)

    def _iter_spooled_encoded_facts(self) -> Iterator[EncodedFactLine]:
        cursor = self._require_connection().execute(
            """
            SELECT object_id, fact_kind, object_name, object_source, object_profile,
                   object_caller, object_callee, linkage, canonical_source, line
            FROM facts
            ORDER BY object_id
            """
        )
        for (
            object_id,
            fact_kind,
            object_name,
            object_source,
            object_profile,
            object_caller,
            object_callee,
            linkage,
            canonical_source,
            line_text,
        ) in cursor:
            yield EncodedFactLine(
                object_id=object_id,
                fact_kind=fact_kind,
                object_name=object_name,
                object_source=object_source,
                object_profile=object_profile,
                object_caller=object_caller,
                object_callee=object_callee,
                canonical_source=canonical_source,
                linkage=linkage,
                line_text=line_text,
            )

    def _spool_unresolved_call(self, evidence: DirectCallEvidence) -> None:
        self._require_connection().execute(
            "INSERT INTO unresolved_calls(line) VALUES (?)",
            (_canonical_spool_json(_direct_call_evidence_to_json(evidence)),),
        )
        self._unresolved_call_count += 1
        if self._unresolved_call_count % STREAMING_SPOOL_COMMIT_INTERVAL == 0:
            self._commit()

    def _iter_spooled_relatives(self) -> Iterator[FactRelative]:
        for relative in self._iter_spooled_encoded_relatives():
            yield _relative_from_encoded_relative_line(relative)

    def _iter_spooled_encoded_relatives(self) -> Iterator[EncodedRelativeLine]:
        cursor = self._require_connection().execute(
            """
            SELECT relative_id, from_fact_id, to_fact_id, relation_kind,
                   condition_json, object_profile, line
            FROM relatives
            ORDER BY relative_id
            """
        )
        for relative_id, from_fact_id, to_fact_id, relation_kind, condition_json, object_profile, line_text in cursor:
            yield EncodedRelativeLine(
                relative_id=relative_id,
                from_fact_id=from_fact_id,
                to_fact_id=to_fact_id,
                relation_kind=relation_kind,
                condition=json.loads(condition_json) if condition_json is not None else None,
                object_profile=object_profile,
                line_text=line_text,
            )

    def _iter_spooled_unresolved_calls(self) -> Iterator[DirectCallEvidence]:
        for (line_text,) in self._require_connection().execute("SELECT line FROM unresolved_calls ORDER BY seq"):
            yield _direct_call_evidence_from_json(json.loads(line_text))

    def _resolve_and_spool_direct_calls(self) -> None:
        started = time.perf_counter()
        self._rebuild_direct_call_index_from_spooled_facts()
        stats = _DirectCallResolutionStats(
            pending_call_count=self._unresolved_call_count,
            function_index_entry_count=len(self._direct_call_index.functions_by_id),
        )
        if self._unresolved_call_count:
            self._commit()
            db_path = self._require_db_path()
            worker_count = min(max(1, self._effective_worker_count), self._unresolved_call_count)
            stats.resolver_worker_count = worker_count
            stats.pending_shard_count = worker_count
            if worker_count <= 1:
                manifests = [
                    _resolve_pending_direct_call_shard_to_segment(
                        db_path,
                        0,
                        worker_count,
                        self._direct_call_index,
                        self.profile,
                        self._resolved_segment_dir(),
                    )
                ]
            else:
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    manifests = list(
                        executor.map(
                            lambda shard_index: _resolve_pending_direct_call_shard_to_segment(
                                db_path,
                                shard_index,
                                worker_count,
                                self._direct_call_index,
                                self.profile,
                                self._resolved_segment_dir(),
                            ),
                            range(worker_count),
                        )
                    )
            use_external_relative_merge = bool(self._relative_segment_manifests)
            for manifest in manifests:
                _merge_direct_call_stats(stats, manifest.stats)
                self._resolved_segment_count += 1
                self._resolved_segment_bytes += manifest.byte_count
                if use_external_relative_merge:
                    self._relative_segment_manifests.append(_relative_segment_from_resolved_manifest(manifest))
                    stats.resolved_call_count += manifest.relative_count
                    continue
                for relative in _iter_segment_encoded_relatives(manifest.relatives_path):
                    if not self._spool_encoded_relative(relative):
                        stats.duplicate_relation_count += 1
                    else:
                        stats.resolved_call_count += 1
        stats.resolver_duration_ms = round(_elapsed_ms(started))
        self.extractor._emit_direct_call_resolution_event(stats, self.profile)

    def _record_relative(self, relative: FactRelative) -> None:
        self._relative_count += 1
        self._relatives_by_kind[relative.relation_kind] += 1

    def _record_encoded_relative(self, relative: EncodedRelativeLine) -> None:
        self._relative_count += 1
        self._relatives_by_kind[relative.relation_kind] += 1

    def _index_function_fact(self, fact: CodeFact) -> None:
        if fact.fact_kind != "function":
            return
        self._index_direct_call_function(_direct_call_function_from_code_fact(fact))

    def _index_direct_call_function(self, fact: _DirectCallFunction) -> None:
        self._direct_call_index.functions_by_id[fact.object_id] = fact
        self._direct_call_index.functions_by_name.setdefault(fact.object_name, []).append(fact)
        source = _direct_call_function_canonical_source(fact)
        if source is not None:
            self._direct_call_index.functions_by_source_name.setdefault((source, fact.object_name), []).append(fact)

    def _rebuild_direct_call_index_from_spooled_facts(self) -> None:
        self._direct_call_index = _DirectCallResolutionIndex(
            functions_by_id={},
            functions_by_name={},
            functions_by_source_name={},
        )
        cursor = self._require_connection().execute(
            """
            SELECT object_id, object_name, object_source, canonical_source, linkage
            FROM facts
            WHERE fact_kind = 'function'
            ORDER BY object_id
            """
        )
        for object_id, object_name, object_source, canonical_source, linkage in cursor:
            self._index_direct_call_function(
                _DirectCallFunction(
                    object_id=object_id,
                    object_name=object_name,
                    object_source=object_source,
                    canonical_source=canonical_source,
                    linkage=linkage,
                )
            )

    def _commit(self) -> None:
        self._require_connection().commit()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("streaming extraction is not open")
        return self._connection

    def _require_db_path(self) -> Path:
        if self._db_path is None:
            raise RuntimeError("streaming extraction reducer database is not open")
        return self._db_path


def _canonical_spool_json(row: Dict[str, JSONValue]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _encoded_fact_from_code_fact(fact: CodeFact) -> EncodedFactLine:
    payload = dict(fact.payload)
    payload["fact_kind"] = fact.fact_kind
    return EncodedFactLine.from_fact_fields(
        object_id=fact.object_id,
        object_name=fact.object_name,
        object_description=fact.object_description,
        object_source=fact.object_source,
        object_profile=fact.object_profile,
        object_caller=fact.object_caller,
        object_callee=fact.object_callee,
        fact_kind=fact.fact_kind,
        payload=payload,
    )


def _code_fact_from_encoded_fact_line(encoded: EncodedFactLine) -> CodeFact:
    row = json.loads(encoded.read_line_text())
    stored = StoredFactLine.from_json(row)
    payload = dict(stored.payload)
    known = {
        "object_id",
        "object_name",
        "object_description",
        "object_source",
        "object_profile",
        "object_caller",
        "object_callee",
    }
    fact_kind = payload.get("fact_kind") if isinstance(payload.get("fact_kind"), str) else stored.fact_kind
    return CodeFact(
        fact_kind=fact_kind,
        object_id=str(payload.get("object_id")),
        object_name=str(payload.get("object_name")),
        object_description=str(payload.get("object_description")),
        object_source=str(payload.get("object_source")),
        object_profile=str(payload.get("object_profile")),
        object_caller=payload.get("object_caller") if isinstance(payload.get("object_caller"), str) else None,
        object_callee=payload.get("object_callee") if isinstance(payload.get("object_callee"), str) else None,
        payload={key: value for key, value in payload.items() if key not in known and key != "fact_kind"},
    )


def _encoded_relative_from_fact_relative(relative: FactRelative) -> EncodedRelativeLine:
    return EncodedRelativeLine.from_relative(relative)


def _relative_from_encoded_relative_line(encoded: EncodedRelativeLine) -> FactRelative:
    row = json.loads(encoded.read_line_text())
    return StoredRelativeLine.from_json(row).to_relative()


def _merge_duplicate_encoded_fact_lines(existing_line: str, candidate_line: str) -> Optional[EncodedFactLine]:
    existing = _code_fact_from_encoded_fact_line(
        EncodedFactLine.from_stored_line(StoredFactLine.from_json(json.loads(existing_line)), line_text=existing_line)
    )
    candidate = _code_fact_from_encoded_fact_line(
        EncodedFactLine.from_stored_line(StoredFactLine.from_json(json.loads(candidate_line)), line_text=candidate_line)
    )
    merged = _merge_duplicate_fact_json(existing.to_json(), candidate.to_json())
    if merged is None:
        return None
    return _encoded_fact_from_code_fact(CodeFact.from_json(merged))


def _merge_duplicate_fact_json(
    existing: Dict[str, JSONValue],
    candidate: Dict[str, JSONValue],
) -> Optional[Dict[str, JSONValue]]:
    if existing == candidate:
        return existing
    if _json_subset(existing, candidate):
        return candidate
    if _json_subset(candidate, existing):
        return existing
    return None


def _json_subset(left: JSONValue, right: JSONValue) -> bool:
    if isinstance(left, dict):
        if not isinstance(right, dict):
            return False
        for key, value in left.items():
            if key not in right or not _json_subset(value, right[key]):
                return False
        return True
    if isinstance(left, list):
        return isinstance(right, list) and left == right
    return left == right


def _gc_stale_map_reduce_runs(run_root: Path) -> int:
    now = time.time()
    removed = 0
    try:
        children = list(run_root.iterdir())
    except OSError:
        return 0
    for child in children:
        if not child.is_dir():
            continue
        try:
            age_seconds = now - child.stat().st_mtime
        except OSError:
            continue
        if age_seconds < MAP_REDUCE_STALE_RUN_TTL_SECONDS:
            continue
        lock_path = child / ".lock"
        if lock_path.exists() and _map_reduce_lock_is_live(lock_path):
            continue
        try:
            shutil.rmtree(child)
        except OSError:
            continue
        removed += 1
    return removed


def _map_reduce_lock_is_live(lock_path: Path) -> bool:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _resolve_pending_direct_call_shard_to_segment(
    db_path: Path,
    shard_index: int,
    shard_count: int,
    index: _DirectCallResolutionIndex,
    profile: str,
    segment_dir: Path,
) -> _ResolvedRelativeSegmentManifest:
    stats = _DirectCallResolutionStats()
    relatives_path = segment_dir / f"direct-call-{shard_index:04d}.jsonl"
    relatives_index_path = segment_dir / f"direct-call-{shard_index:04d}.index"
    relatives: List[FactRelative] = []
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            "SELECT line FROM unresolved_calls WHERE (seq % ?) = ? ORDER BY seq",
            (shard_count, shard_index),
        )
        for (line_text,) in cursor:
            evidence = _direct_call_evidence_from_json(json.loads(line_text))
            stats.pending_call_count += 1
            caller = index.functions_by_id.get(evidence.caller_fact_id)
            if caller is None:
                stats.missing_caller_count += 1
                continue
            resolved = _select_direct_call_target(index, evidence, caller, stats)
            if resolved is None:
                continue
            target, strategy = resolved
            relatives.append(_make_resolved_direct_call_relative(caller, target, evidence, strategy, profile))
    finally:
        connection.close()
    relative_write = _write_encoded_relative_rows(
        relatives_path,
        relatives_index_path,
        relatives,
    )
    return _ResolvedRelativeSegmentManifest(
        relatives_path=relatives_path,
        relatives_index_path=relatives_index_path,
        stats=stats,
        relative_count=relative_write.relative_count,
        relative_line_bytes=relative_write.relative_line_bytes,
        relative_index_bytes=relative_write.relative_index_bytes,
        byte_count=relative_write.relative_line_bytes + relative_write.relative_index_bytes,
    )


def _stderr_has_clang_error(stderr: str) -> bool:
    return re.search(r"(?im)(?:fatal error|error):", stderr) is not None


def _partial_ast_reason(returncode: int, stderr: str) -> str:
    has_stderr_error = _stderr_has_clang_error(stderr)
    if returncode != 0 and has_stderr_error:
        return "nonzero_exit_and_stderr_error"
    if returncode != 0:
        return "nonzero_exit"
    return "stderr_error"


def _ast_command_timeout_seconds(path: Path) -> int:
    timeout = AST_COMMAND_TIMEOUT_SECONDS
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return timeout
    extra_steps = size_bytes // AST_COMMAND_TIMEOUT_SIZE_STEP_BYTES
    if extra_steps > 0:
        timeout += extra_steps * AST_COMMAND_TIMEOUT_SECONDS_PER_STEP
    return min(timeout, AST_COMMAND_TIMEOUT_MAX_SECONDS)


def _diagnostic_details(
    diagnostic_kind: str,
    diagnostic_reason: Optional[str] = None,
    extra: Optional[Dict[str, JSONValue]] = None,
) -> Dict[str, JSONValue]:
    details: Dict[str, JSONValue] = {"diagnostic_kind": diagnostic_kind}
    if diagnostic_reason is not None and diagnostic_reason != "ok":
        details["reason"] = diagnostic_reason
    if extra:
        details.update(extra)
    return details

__all__ = [name for name in globals() if not name.startswith("__")]

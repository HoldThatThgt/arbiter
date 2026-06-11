from __future__ import annotations

import base64
import binascii
import heapq
import json
import os
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

try:
    import resource
except ImportError:  # pragma: no cover - Windows compatibility
    resource = None  # type: ignore[assignment]

from cipher2.common import JSONValue
from cipher2.storage import EncodedFactLine, EncodedRelativeLine, FactRelative, StoredFactLine, StoredRelativeLine

from .constants import (
    MAP_REDUCE_STALE_RUN_TTL_SECONDS,
    RELATIVE_MERGE_DEFAULT_FAN_IN,
    RELATIVE_MERGE_FD_HEADROOM,
    RELATIVE_MERGE_FD_PER_SEGMENT,
    RELATIVE_MERGE_MIN_FAN_IN,
    WORKER_RELATIVE_DEDUP_ENTRY_OVERHEAD_BYTES,
    WORKER_RELATIVE_DEDUP_MAX_ESTIMATED_BYTES,
)
from .direct_calls import _direct_call_evidence_from_json, _direct_call_evidence_to_json
from .mapper_utils import _elapsed_ms, _hash_text
from .models import (
    CodeFact,
    DirectCallEvidence,
    _FileMapResult,
    _FileWorkItem,
    _IndexedRelativeLine,
    _MapSegmentManifest,
    _RelativeExternalMergeStats,
    _RelativeIndexEntry,
    _RelativeSegmentManifest,
    _ResolvedRelativeSegmentManifest,
)
from .toolchain import _make_init_error


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

__all__ = [name for name in globals() if not name.startswith("__")]

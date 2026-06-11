from __future__ import annotations

import json
import multiprocessing
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from cipher2.common import JSONValue
from cipher2.config import CipherConfig
from cipher2.initializer.progress import InitProgressEvent, InitProgressSink
from cipher2.storage import (
    EncodedFactLine,
    EncodedRelativeLine,
    FactRelative,
    SourceInventoryEntry,
)
from cipher2.tools.log import LogError, LogEvent, open_log

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
    _select_direct_call_target,
)
from .streaming_segments import (
    _WorkerRelativeDeduper,
    _canonical_spool_json,
    _code_fact_from_encoded_fact_line,
    _encoded_fact_from_code_fact,
    _encoded_relative_from_fact_relative,
    _gc_stale_map_reduce_runs,
    _iter_external_merged_relative_segments,
    _iter_segment_encoded_facts,
    _iter_segment_encoded_relatives,
    _iter_segment_unresolved_calls,
    _merge_duplicate_encoded_fact_lines,
    _merge_duplicate_fact_json,
    _relative_from_encoded_relative_line,
    _relative_segment_from_map_manifest,
    _relative_segment_from_resolved_manifest,
    _write_encoded_relative_rows,
    _write_file_map_segments,
)

_PROCESS_FILE_WORKER_EXTRACTOR: Optional["CodeFactExtractor"] = None
_PROCESS_FILE_WORKER_HEADER_CACHE: Optional[_HeaderMaterializationCache] = None
_PROCESS_FILE_WORKER_RELATIVE_DEDUPER: Optional["_WorkerRelativeDeduper"] = None


@dataclass
class _ManagedFileWorker:
    worker_id: int
    generation: int
    process: Any
    task_queue: Any
    active_item: Optional[_FileWorkItem] = None
    active_started: Optional[float] = None
    active_deadline: Optional[float] = None
    active_timeout_seconds: Optional[int] = None


@dataclass(frozen=True)
class _ManagedWorkerResult:
    worker_id: int
    generation: int
    seq: int
    outcome: Optional[_FileWorkOutcome] = None
    init_error_code: Optional[str] = None
    init_error_message: Optional[str] = None
    init_error_source: Optional[str] = None
    init_error_details: Dict[str, JSONValue] = field(default_factory=dict)
    exception_message: Optional[str] = None


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


def _managed_file_worker_loop(
    target_repo: Path,
    config: CipherConfig,
    backend_spec: _ProcessWorkerBackendSpec,
    task_queue: Any,
    result_queue: Any,
    worker_id: int,
    generation: int,
) -> None:
    try:
        _initialize_process_file_worker(target_repo, config, backend_spec)
    except Exception as exc:
        result_queue.put(_managed_worker_result_from_exception(worker_id, generation, -1, exc))
        return
    while True:
        item = task_queue.get()
        if item is None:
            return
        try:
            outcome = _run_file_work_item_in_process(item)
        except Exception as exc:
            result_queue.put(_managed_worker_result_from_exception(worker_id, generation, item.seq, exc))
        else:
            result_queue.put(
                _ManagedWorkerResult(
                    worker_id=worker_id,
                    generation=generation,
                    seq=item.seq,
                    outcome=outcome,
                )
            )


def _managed_worker_result_from_exception(
    worker_id: int,
    generation: int,
    seq: int,
    exc: Exception,
) -> _ManagedWorkerResult:
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        details = getattr(exc, "details", None)
        return _ManagedWorkerResult(
            worker_id=worker_id,
            generation=generation,
            seq=seq,
            init_error_code=code,
            init_error_message=str(getattr(exc, "message", str(exc))),
            init_error_source=getattr(exc, "source", None) if isinstance(getattr(exc, "source", None), str) else None,
            init_error_details=dict(details) if isinstance(details, dict) else {},
        )
    return _ManagedWorkerResult(
        worker_id=worker_id,
        generation=generation,
        seq=seq,
        exception_message=f"{exc.__class__.__name__}: {exc}",
    )


def _multiprocessing_context():
    try:
        return multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - Windows compatibility
        return multiprocessing.get_context()


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
    if not isinstance(ast, dict):
        ast = getattr(backend, "_ast_by_rel", None)
    if isinstance(ast, dict) and extractor.toolchain_probe_result is not None:
        return _ProcessWorkerBackendSpec(
            kind="in_memory_ast",
            in_memory_ast=ast,
            toolchain_probe_result=extractor.toolchain_probe_result,
        )
    if _ast_backend_module._TEST_AST_BACKEND_FACTORY is not None:
        return _ProcessWorkerBackendSpec(kind="json_test")
    return _ProcessWorkerBackendSpec(kind="default")


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
        self._worker_timeout_count = 0
        self._worker_restart_count = 0
        self._worker_crash_count = 0
        self._reduce_duration_ms = 0.0
        self._reduce_outcome_count = 0
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
        run_root = self.extractor.target_repo / ".cipher" / "run" / "initializer-mapreduce"
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
            collect_started = started
            self.extractor.compile_command_index = self.extractor._load_compile_command_index()
            self._sources = self.extractor._collect_source_files(self.source_roots)
            self.source_count = len(self._sources)
            collect_payload = {
                "profile": self.profile,
                "compile_database_configured": self.extractor.compile_command_index is not None,
            }
            self.extractor._emit_progress_event(
                "sources_planned",
                started=started,
                total=self.source_count,
                payload=collect_payload,
            )
            self.extractor._emit_init_stage(
                "collect",
                started=collect_started,
                counts={
                    "source_count": self.source_count,
                    "compile_database_configured_count": 1 if self.extractor.compile_command_index is not None else 0,
                },
                payload=collect_payload,
            )
            extraction_started = time.perf_counter()
            if self._sources:
                self.extractor._validate_toolchain()
            active_worker_count = min(
                max(1, self.extractor.config.extractor_worker_count),
                max(1, self.source_count),
            )
            self._effective_worker_count = active_worker_count
            if self._sources:
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
                worker_timeout_count=self._worker_timeout_count,
                worker_restart_count=self._worker_restart_count,
                worker_crash_count=self._worker_crash_count,
            )
            self.extractor._emit_init_stage(
                "extract",
                started=extraction_started,
                counts={
                    "source_count": self.source_count,
                    "worker_count": self._effective_worker_count,
                    "successful_file_count": len(self._successful_sources),
                    "skipped_file_count": self.source_count - len(self._successful_sources),
                    "warning_count": len(self.errors),
                    "partial_ast_count": sum(1 for error in self.errors if getattr(error, "code", None) == "clang_ast_partial"),
                    "map_output_segment_count": self._map_segment_count,
                    "map_output_bytes": self._map_segment_bytes,
                    "worker_timeout_count": self._worker_timeout_count,
                    "worker_restart_count": self._worker_restart_count,
                    "worker_crash_count": self._worker_crash_count,
                },
                payload={
                    "profile": self.profile,
                    "mode": "serial" if self._effective_worker_count <= 1 else "bounded_pool",
                    "window": "worker_pool_wall_clock",
                },
            )
            self.extractor._emit_init_stage(
                "reduce",
                duration_ms=self._reduce_duration_ms,
                counts={
                    "reduce_outcome_count": self._reduce_outcome_count,
                    "fact_count": self._fact_count,
                    "fact_duplicate_exact_count": self._fact_duplicate_exact_count,
                    "fact_duplicate_merge_parse_count": self._fact_duplicate_merge_parse_count,
                    "fact_duplicate_conflict_count": self._fact_duplicate_conflict_count,
                    "fact_line_reencoded_count": self._fact_line_reencoded_count,
                    "relative_map_input_count": self._relative_map_input_count,
                    "relative_map_written_count": self._relative_map_written_count,
                    "relative_map_skipped_exact_count": self._relative_map_skipped_exact_count,
                },
                payload={
                    "profile": self.profile,
                    "mode": "per_file_outcome_accumulator",
                    "window": "cumulative_outcome_merge",
                },
            )
            self._commit()
            self._facts_finished = True
            yield from self._iter_spooled_encoded_facts()
        except Exception:
            self._facts_finished = False
            raise

    def _extract_source_facts(self, seq: int, source: Path) -> None:
        outcome = self._run_file_work_item(self._make_work_item(seq, source))
        self._merge_file_outcome(outcome)

    def _iter_parallel_source_facts(self, worker_count: int) -> None:
        next_submit = 0
        backend_spec = _process_worker_backend_spec(self.extractor)
        context = _multiprocessing_context()
        result_queue = context.Queue()
        workers: List[_ManagedFileWorker] = []
        try:
            workers = [
                self._start_managed_file_worker(context, result_queue, backend_spec, worker_id=index + 1, generation=1)
                for index in range(worker_count)
            ]
        except OSError:
            self._effective_worker_count = 1
            for seq, source in enumerate(self._sources):
                self._extract_source_facts(seq, source)
            return
        try:
            while next_submit < len(self._sources) or any(worker.active_item is not None for worker in workers):
                next_submit = self._submit_available_managed_work(workers, next_submit)
                self._receive_managed_results(result_queue, workers, block=True)
                self._handle_managed_worker_deadlines(workers, context, result_queue, backend_spec)
        finally:
            for worker in workers:
                self._stop_managed_file_worker(worker)

    def _start_managed_file_worker(
        self,
        context,
        result_queue,
        backend_spec: _ProcessWorkerBackendSpec,
        *,
        worker_id: int,
        generation: int,
    ) -> _ManagedFileWorker:
        task_queue = context.Queue()
        process = context.Process(
            target=_managed_file_worker_loop,
            args=(
                self.extractor.target_repo,
                self.extractor.config,
                backend_spec,
                task_queue,
                result_queue,
                worker_id,
                generation,
            ),
        )
        process.start()
        return _ManagedFileWorker(
            worker_id=worker_id,
            generation=generation,
            process=process,
            task_queue=task_queue,
        )

    def _submit_available_managed_work(self, workers: List[_ManagedFileWorker], next_submit: int) -> int:
        for worker in workers:
            if next_submit >= len(self._sources):
                break
            if worker.active_item is not None:
                continue
            item = self._make_work_item(next_submit, self._sources[next_submit])
            started = time.perf_counter()
            timeout_seconds = _ast_command_timeout_seconds(item.source)
            worker.active_item = item
            worker.active_started = started
            worker.active_timeout_seconds = timeout_seconds
            worker.active_deadline = started + timeout_seconds
            worker.task_queue.put(item)
            next_submit += 1
        return next_submit

    def _receive_managed_results(
        self,
        result_queue,
        workers: List[_ManagedFileWorker],
        *,
        block: bool,
    ) -> None:
        timeout = self._managed_result_wait_seconds(workers) if block else 0
        try:
            result = result_queue.get(timeout=timeout) if block else result_queue.get_nowait()
        except queue.Empty:
            return
        self._handle_managed_result(result, workers)
        while True:
            try:
                result = result_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_managed_result(result, workers)

    def _managed_result_wait_seconds(self, workers: Sequence[_ManagedFileWorker]) -> float:
        deadlines = [
            worker.active_deadline
            for worker in workers
            if worker.active_item is not None and worker.active_deadline is not None
        ]
        if not deadlines:
            return 0.05
        return max(0.0, min(0.05, min(deadlines) - time.perf_counter()))

    def _handle_managed_result(self, result: _ManagedWorkerResult, workers: List[_ManagedFileWorker]) -> None:
        worker = self._find_managed_worker(workers, result.worker_id, result.generation)
        if worker is None:
            return
        if result.init_error_code is not None:
            raise _make_init_error(
                result.init_error_code,
                result.init_error_message or result.init_error_code,
                source=result.init_error_source,
                details=result.init_error_details,
            )
        if result.exception_message is not None:
            raise RuntimeError(result.exception_message)
        if result.outcome is None:
            raise RuntimeError("managed file worker returned no outcome")
        if worker.active_item is None or worker.active_item.seq != result.seq:
            return
        worker.active_item = None
        worker.active_started = None
        worker.active_deadline = None
        worker.active_timeout_seconds = None
        self._merge_file_outcome(result.outcome)

    def _handle_managed_worker_deadlines(
        self,
        workers: List[_ManagedFileWorker],
        context,
        result_queue,
        backend_spec: _ProcessWorkerBackendSpec,
    ) -> None:
        self._receive_managed_results(result_queue, workers, block=False)
        now = time.perf_counter()
        for index, worker in enumerate(list(workers)):
            item = worker.active_item
            if item is not None and worker.active_deadline is not None and now >= worker.active_deadline:
                timeout_seconds = worker.active_timeout_seconds or _ast_command_timeout_seconds(item.source)
                started = worker.active_started or now
                self._terminate_managed_file_worker(worker)
                self._cleanup_abandoned_segment(item)
                self._worker_timeout_count += 1
                self._worker_restart_count += 1
                self._merge_file_outcome(
                    self._managed_worker_warning_outcome(
                        item,
                        started,
                        "clang AST invocation timed out",
                        diagnostic_kind="timeout",
                        diagnostic_reason="timeout",
                        diagnostic_details={"timeout_seconds": timeout_seconds},
                    )
                )
                workers[index] = self._start_managed_file_worker(
                    context,
                    result_queue,
                    backend_spec,
                    worker_id=worker.worker_id,
                    generation=worker.generation + 1,
                )
                continue
            if item is not None and not worker.process.is_alive():
                started = worker.active_started or now
                exitcode = worker.process.exitcode
                self._cleanup_abandoned_segment(item)
                self._worker_crash_count += 1
                self._worker_restart_count += 1
                self._merge_file_outcome(
                    self._managed_worker_warning_outcome(
                        item,
                        started,
                        "clang AST worker crashed",
                        diagnostic_kind="unknown",
                        diagnostic_reason="worker_crash",
                        diagnostic_details={"worker_exitcode": exitcode} if isinstance(exitcode, int) else {},
                    )
                )
                workers[index] = self._start_managed_file_worker(
                    context,
                    result_queue,
                    backend_spec,
                    worker_id=worker.worker_id,
                    generation=worker.generation + 1,
                )
                continue
            if item is None and not worker.process.is_alive():
                self._worker_restart_count += 1
                workers[index] = self._start_managed_file_worker(
                    context,
                    result_queue,
                    backend_spec,
                    worker_id=worker.worker_id,
                    generation=worker.generation + 1,
                )

    def _managed_worker_warning_outcome(
        self,
        item: _FileWorkItem,
        started: float,
        message: str,
        *,
        diagnostic_kind: str,
        diagnostic_reason: str,
        diagnostic_details: Optional[Dict[str, JSONValue]] = None,
    ) -> _FileWorkOutcome:
        return _FileWorkOutcome(
            seq=item.seq,
            source=item.source,
            rel_source=item.rel_source,
            profile=item.profile,
            compile_lookup=item.compile_lookup,
            started=started,
            error_code="clang_ast_failed",
            error_message=message,
            diagnostic_kind=diagnostic_kind,
            diagnostic_reason=diagnostic_reason,
            diagnostic_details=dict(diagnostic_details or {}),
        )

    def _find_managed_worker(
        self,
        workers: Sequence[_ManagedFileWorker],
        worker_id: int,
        generation: int,
    ) -> Optional[_ManagedFileWorker]:
        for worker in workers:
            if worker.worker_id == worker_id and worker.generation == generation:
                return worker
        return None

    def _cleanup_abandoned_segment(self, item: _FileWorkItem) -> None:
        if item.segment_dir is not None:
            shutil.rmtree(item.segment_dir, ignore_errors=True)

    def _terminate_managed_file_worker(self, worker: _ManagedFileWorker) -> None:
        if worker.process.is_alive():
            worker.process.terminate()
            worker.process.join(timeout=0.25)
        if worker.process.is_alive() and hasattr(worker.process, "kill"):
            worker.process.kill()
            worker.process.join(timeout=0.25)
        if worker.process.is_alive():
            worker.process.terminate()
        self._close_managed_worker_queues(worker)

    def _stop_managed_file_worker(self, worker: _ManagedFileWorker) -> None:
        if worker.process.is_alive():
            try:
                worker.task_queue.put(None)
            except Exception:
                pass
            worker.process.join(timeout=0.25)
        if worker.process.is_alive():
            worker.process.terminate()
            worker.process.join(timeout=0.25)
        if worker.process.is_alive() and hasattr(worker.process, "kill"):
            worker.process.kill()
            worker.process.join(timeout=0.25)
        self._close_managed_worker_queues(worker)

    def _close_managed_worker_queues(self, worker: _ManagedFileWorker) -> None:
        for close_name in ("close",):
            close = getattr(worker.task_queue, close_name, None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        join_thread = getattr(worker.task_queue, "join_thread", None)
        if join_thread is not None:
            try:
                join_thread()
            except Exception:
                pass

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
        reduce_started = time.perf_counter()
        try:
            self._merge_file_outcome_inner(outcome)
        finally:
            self._reduce_duration_ms += _elapsed_ms(reduce_started)
            self._reduce_outcome_count += 1

    def _merge_file_outcome_inner(self, outcome: _FileWorkOutcome) -> None:
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
                    self.extractor._emit_init_stage(
                        "relative_merge",
                        duration_ms=stats.duration_ms,
                        counts={
                            **stats.to_counts(),
                            "relative_count": self._relative_count,
                        },
                        payload={
                            "profile": self.profile,
                            "mode": "external_k_way",
                            "window": "snapshot_consumed_iterator",
                        },
                    )
            else:
                merge_started = time.perf_counter()
                for relative in self._iter_spooled_encoded_relatives():
                    yield relative
                self.extractor._emit_init_stage(
                    "relative_merge",
                    started=merge_started,
                    counts={
                        "relative_count": self._relative_count,
                        "relative_merge_input_count": self._relative_count,
                        "relative_merge_accepted_count": self._relative_count,
                        "relative_merge_segment_count": 0,
                    },
                    payload={
                        "profile": self.profile,
                        "mode": "sqlite_spool",
                        "window": "snapshot_consumed_iterator",
                    },
                )
        except Exception:
            self._relatives_finished = False
            raise

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
        self.extractor._emit_init_stage(
            "resolve",
            duration_ms=float(stats.resolver_duration_ms),
            counts=stats.to_counts(),
            payload={"profile": self.profile, "window": "direct_call_resolver_wall_clock"},
        )

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

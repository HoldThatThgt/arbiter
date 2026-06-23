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
from dataclasses import FrozenInstanceError, dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

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
from .toolchain import *
from . import ast_backend as _ast_backend_module
from .ast_backend import *
from .mapper import _ClangAstMapper

class CodeFactExtractor:
    def __init__(
        self,
        target_repo: Path,
        config: CipherConfig,
        *,
        log_enabled: bool = False,
        progress_sink: Optional[InitProgressSink] = None,
    ) -> None:
        self.target_repo = Path(target_repo)
        self.config = config
        self.log_enabled = log_enabled
        self.progress_sink = progress_sink
        self.toolchain_probe_result: Optional[ToolchainProbeResult] = None
        self._ast_backend: Optional["_AstBackend"] = None
        self.compile_command_index: Optional[_CompileCommandIndex] = None

    def extract(self, source_roots: Optional[Sequence[Union[str, Path]]], profile: str) -> Iterator[CodeFact]:
        with self.stream(source_roots, profile) as extraction:
            yield from extraction.facts

    def collect(self, source_roots: Optional[Sequence[Union[str, Path]]], profile: str) -> ExtractionResult:
        with self.stream(source_roots, profile) as extraction:
            facts = list(extraction.facts)
            relatives = list(extraction.relatives)
            source_inventory = list(extraction.source_inventory)
            return ExtractionResult(
                facts=facts,
                relatives=relatives,
                source_inventory=source_inventory,
                unresolved_calls=extraction.unresolved_calls,
                source_count=extraction.source_count,
                errors=extraction.errors,
            )

    def stream(self, source_roots: Optional[Sequence[Union[str, Path]]], profile: str) -> "_StreamingExtraction":
        return _StreamingExtraction(self, source_roots, profile)

    def extract_dirty_sources(self, dirty_sources, profile: str):
        from ._shim import IncrementalBuildResult

        roots = [item.rel_path for item in dirty_sources]
        result = self.collect(roots, profile)
        return IncrementalBuildResult(
            facts=[fact.to_fact_record() for fact in result.facts],
            relatives=result.relatives,
            source_inventory=result.source_inventory,
        )

    def _load_compile_command_index(self) -> Optional[_CompileCommandIndex]:
        path = self.config.compile_database_path
        if path is None:
            return None
        started = time.perf_counter()
        stats = _CompileCommandStats()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._emit_compile_database_event(stats, "error", started, "malformed_compile_database")
            raise _make_init_error("malformed_compile_database", "compile database must be valid JSON") from exc
        if not isinstance(data, list):
            self._emit_compile_database_event(stats, "error", started, "malformed_compile_database")
            raise _make_init_error("malformed_compile_database", "compile database must be a JSON list")
        stats.entry_count = len(data)
        by_source: Dict[Path, _CompileCommandEntry] = {}
        for item in data:
            if not isinstance(item, dict):
                stats.malformed_entry_count += 1
                self._emit_compile_database_event(stats, "error", started, "malformed_compile_database")
                raise _make_init_error("malformed_compile_database", "compile database entries must be JSON objects")
            try:
                entry = _compile_command_entry_from_mapping(self.target_repo, path, item)
            except _MalformedCompileDatabaseError as exc:
                stats.malformed_entry_count += 1
                self._emit_compile_database_event(stats, "error", started, "malformed_compile_database")
                raise _make_init_error("malformed_compile_database", exc.message) from exc
            if entry is None:
                stats.ignored_outside_repo_count += 1
                continue
            if entry.source_path in by_source:
                stats.duplicate_source_count += 1
                continue
            by_source[entry.source_path] = entry
            stats.indexed_source_count += 1
            stats.stripped_argument_count += entry.stripped_argument_count
        index = _CompileCommandIndex(self.target_repo, path, by_source, stats)
        self._emit_compile_database_event(stats, "ok", started, None)
        return index

    def _lookup_compile_command(self, source: Path) -> _CompileCommandLookup:
        if self.compile_command_index is None:
            return _CompileCommandLookup(
                configured=False,
                matched=False,
                entry=None,
                flags=[],
                command_hash=None,
                argument_count=0,
                stripped_argument_count=0,
            )
        return self.compile_command_index.lookup(source)

    def _validate_toolchain(self) -> None:
        started = time.perf_counter()
        try:
            clang = _resolve_executable(self.config.clang_executable, "clang", "clang_unavailable")
            if _ast_backend_module._TEST_AST_BACKEND_FACTORY is not None:
                backend = _ast_backend_module._TEST_AST_BACKEND_FACTORY(self, clang)
            else:
                backend = _LibclangAstBackend(
                    clang_executable=clang,
                    clang_args=self.config.clang_args,
                    target_repo=self.target_repo,
                    configured_library=self.config.libclang_library_path,
                )
            result = backend.probe()
        except Exception as exc:
            if isinstance(exc, _CapabilityProbeError):
                details = {"missing_evidence": ",".join(exc.missing_evidence)} if exc.missing_evidence else None
                error = _make_init_error("clang_capability_failed", exc.message, details=details)
                missing_evidence = exc.missing_evidence
            elif isinstance(exc, _LibclangVersionMismatchError):
                error = _make_init_error(
                    "libclang_version_mismatch",
                    "libclang version must match clang executable major version",
                    details={
                        "clang_version": exc.clang_version or "",
                        "libclang_version": exc.libclang_version or "",
                    },
                )
                missing_evidence = []
            elif isinstance(exc, _LibclangUnavailableError):
                error = _make_init_error("libclang_unavailable", "libclang library is unavailable", details={"reason": exc.reason})
                missing_evidence = []
            elif hasattr(exc, "code") and getattr(exc, "code") == "clang_unavailable":
                error = exc
                missing_evidence = []
            else:
                error = _make_init_error("clang_capability_failed", "clang capability probe failed")
                missing_evidence = []
            self._emit_toolchain_event(
                status="error",
                started=started,
                error_code=error.code,
                result=None,
                missing_evidence=missing_evidence,
            )
            raise error
        self.toolchain_probe_result = result
        self._ast_backend = backend
        self._emit_toolchain_event(
            status="warning" if result.warning_codes else "ok",
            started=started,
            error_code=None,
            result=result,
            missing_evidence=[],
        )

    def _collect_source_files(self, source_roots: Optional[Sequence[Union[str, Path]]]) -> List[Path]:
        target_resolved = self.target_repo.resolve(strict=False)
        roots: Sequence[Union[str, Path]] = [self.target_repo] if source_roots is None else source_roots
        if not isinstance(roots, Sequence) or isinstance(roots, (str, bytes, Path)):
            raise _make_init_error("invalid_source_root", "source_roots must be a list of paths")
        compile_db_sources = self._compile_database_source_set(target_resolved)
        files: Set[Path] = set()
        for root in roots:
            candidate = Path(root)
            candidate = candidate if candidate.is_absolute() else self.target_repo / candidate
            resolved = candidate.resolve(strict=False)
            if not _is_relative_to(resolved, target_resolved):
                raise _make_init_error("path_escape", "source root escapes target repository")
            if not candidate.exists():
                raise _make_init_error("invalid_source_root", "source root does not exist")
            if _is_cipher_path(resolved, target_resolved):
                raise _make_init_error("invalid_source_root", "source root cannot be inside .arbiter")
            if candidate.is_file():
                if candidate.suffix.lower() in SOURCE_EXTENSIONS:
                    if compile_db_sources is None or resolved in compile_db_sources:
                        files.add(resolved)
                continue
            if not candidate.is_dir():
                raise _make_init_error("invalid_source_root", "source root must be a file or directory")
            if compile_db_sources is not None:
                for source in compile_db_sources:
                    if _is_relative_to(source, resolved):
                        files.add(source)
                continue
            for path in candidate.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
                    continue
                path_resolved = path.resolve(strict=False)
                if _is_relative_to(path_resolved, target_resolved) and not _is_cipher_path(path_resolved, target_resolved):
                    files.add(path_resolved)
        return sorted(files, key=lambda item: _relative_source(self.target_repo, item))

    def _compile_database_source_set(self, target_resolved: Path) -> Optional[Set[Path]]:
        if self.compile_command_index is None:
            return None
        return {
            source
            for source in self.compile_command_index.by_source
            if source.suffix.lower() in SOURCE_EXTENSIONS
            and source.is_file()
            and _is_relative_to(source, target_resolved)
            and not _is_cipher_path(source, target_resolved)
        }

    def _collect_inventory_source_files(self, sources: Sequence[Path]) -> List[Path]:
        target_resolved = self.target_repo.resolve(strict=False)
        files: Set[Path] = set(sources)
        queue = list(sources)
        while queue:
            source = queue.pop(0)
            for included_rel in _extract_include_paths(self.target_repo, source):
                included = (self.target_repo / included_rel).resolve(strict=False)
                if included in files or included.suffix.lower() not in HEADER_EXTENSIONS:
                    continue
                if not included.is_file():
                    continue
                if not _is_relative_to(included, target_resolved) or _is_cipher_path(included, target_resolved):
                    continue
                files.add(included)
                queue.append(included)
        return sorted(files, key=lambda item: _relative_source(self.target_repo, item))

    def _extract_file(
        self,
        path: Path,
        rel_source: str,
        profile: str,
        source_id: str,
        compile_lookup: _CompileCommandLookup,
        *,
        header_resolver_seed: Optional[_HeaderResolverSeed] = None,
        header_context_hash: str = "",
        header_materialization_stats: Optional[_HeaderMaterializationStats] = None,
    ) -> _FileMapResult:
        if self._ast_backend is None:
            raise _make_init_error("clang_capability_failed", "clang capability probe must run before extraction")
        load_result = self._ast_backend.load_ast(path, rel_source, compile_lookup)
        compile_directory = (
            compile_lookup.entry.directory_path
            if compile_lookup.matched and compile_lookup.entry is not None
            else None
        )
        traverse_started = time.perf_counter()
        mapper = _ClangAstMapper(
            self.target_repo,
            rel_source,
            path.suffix.lower().lstrip("."),
            profile,
            source_id,
            compile_directory=compile_directory,
            header_resolver_seed=header_resolver_seed,
            header_context_hash=header_context_hash,
        )
        try:
            file_result = mapper.map(load_result.ast)
        except _RecoverableExtractError:
            raise
        except RecursionError as exc:
            # A translation unit whose AST nests deeper than Python's recursion
            # limit (e.g. a huge generated-parser expression) must not abort the
            # whole snapshot. Convert it to a recoverable error so streaming
            # records the failure and skips just this TU (ADR-0020 hard stop is
            # reserved for genuinely unrecoverable indexer faults).
            raise _RecoverableExtractError(
                "map_failed",
                "translation unit mapping exceeded recursion limit",
                diagnostic_kind="map_error",
                diagnostic_reason="recursion_limit",
                details={"rel_source": rel_source},
            ) from exc
        except Exception as exc:
            # Defence in depth: any unexpected per-file mapping failure degrades
            # to skipping one TU rather than failing the entire extraction.
            raise _RecoverableExtractError(
                "map_failed",
                "translation unit mapping failed",
                diagnostic_kind="map_error",
                diagnostic_reason="mapping_exception",
                details={"rel_source": rel_source, "exception": type(exc).__name__},
            ) from exc
        traverse_duration_ms = _elapsed_ms(traverse_started)
        if header_materialization_stats is not None:
            file_result = replace(
                file_result,
                stats=replace(
                    file_result.stats,
                    header_decl_cache_entry_count=header_materialization_stats.header_decl_cache_entry_count,
                    header_decl_cache_hit_count=header_materialization_stats.header_decl_cache_hit_count,
                    header_decl_cache_miss_count=header_materialization_stats.header_decl_cache_miss_count,
                    header_decl_skipped_subtree_count=header_materialization_stats.header_decl_skipped_subtree_count,
                    header_decl_seed_count=file_result.stats.header_decl_seed_count,
                ),
            )
        file_result = replace(
            file_result,
            backend=load_result.backend,
            parse_duration_ms=load_result.parse_duration_ms,
            traverse_duration_ms=traverse_duration_ms,
        )
        if load_result.warning_code is None:
            return file_result
        stats = replace(
            file_result.stats,
            partial_ast_count=1,
            warning_count=file_result.stats.warning_count + 1,
        )
        return _FileMapResult(
            facts=file_result.facts,
            relatives=file_result.relatives,
            unresolved_calls=file_result.unresolved_calls,
            stats=stats,
            ast_diagnostic_kind=load_result.diagnostic_kind,
            ast_diagnostic_reason=load_result.diagnostic_reason,
            warning_code=load_result.warning_code,
            backend=load_result.backend,
            parse_duration_ms=load_result.parse_duration_ms,
            traverse_duration_ms=traverse_duration_ms,
            header_context_hash=file_result.header_context_hash,
            header_decl_keys=file_result.header_decl_keys,
            header_resolver_seed=file_result.header_resolver_seed,
        )

    def _extract_file_work_item(
        self,
        item: _FileWorkItem,
        header_cache: _HeaderMaterializationCache,
    ) -> _FileMapResult:
        if type(self)._extract_file is not CodeFactExtractor._extract_file:
            return self._extract_file(
                item.source,
                item.rel_source,
                item.profile,
                item.source_id,
                item.compile_lookup,
            )
        context_hash = self._header_materialization_context_hash(item.compile_lookup, item.profile)
        visible_keys, seed = header_cache.visible_state(item.seq, context_hash)
        context = _HeaderMaterializationContext(
            cache=header_cache,
            source_seq=item.seq,
            rel_source=item.rel_source,
            context_hash=context_hash,
            visible_keys=visible_keys,
        )
        if self._ast_backend is None:
            raise _make_init_error("clang_capability_failed", "clang capability probe must run before extraction")
        with self._ast_backend.header_materialization_context(context):
            result = self._extract_file(
                item.source,
                item.rel_source,
                item.profile,
                item.source_id,
                item.compile_lookup,
                header_resolver_seed=seed,
                header_context_hash=context_hash,
                header_materialization_stats=context.stats,
            )
        return replace(
            result,
            stats=replace(
                result.stats,
                header_decl_cache_entry_count=header_cache.entry_count(),
                header_decl_seed_count=seed.fact_count(),
            ),
        )

    def _header_materialization_context_hash(self, compile_lookup: _CompileCommandLookup, profile: str) -> str:
        flags = compile_lookup.flags if compile_lookup.matched else self.config.clang_args
        payload = {
            "profile": profile,
            "flags": list(flags),
            "toolchain_probe": self.toolchain_probe_result_to_digest(),
        }
        return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def _build_source_inventory(
        self,
        sources: List[Path],
        profile: str,
        compile_lookup_by_source: Dict[Path, _CompileCommandLookup],
    ) -> List[SourceInventoryEntry]:
        by_rel = {_relative_source(self.target_repo, source): source for source in sources}
        source_ids = {rel: _source_id(rel, profile) for rel in by_rel}
        includes_by_rel: Dict[str, List[str]] = {}
        included_by: Dict[str, List[str]] = {source_id: [] for source_id in source_ids.values()}
        for rel, source in by_rel.items():
            includes = []
            for included_rel in _extract_include_paths(self.target_repo, source):
                included_id = source_ids.get(included_rel)
                if included_id is not None:
                    includes.append(included_id)
                    included_by[included_id].append(source_ids[rel])
            includes_by_rel[rel] = sorted(set(includes))
        entries = []
        toolchain_hash = _hash_text(
            json.dumps(
                {
                    "clang": self.config.clang_executable,
                    "gcc": self.config.gcc_executable,
                    "clang_args": self.config.clang_args,
                    "profile": profile,
                    "toolchain_probe": self.toolchain_probe_result_to_digest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        global_compile_command_hash = self._global_compile_command_hash()
        for rel, source in sorted(by_rel.items()):
            stat = source.stat()
            source_id = source_ids[rel]
            compile_lookup = compile_lookup_by_source.get(source)
            compile_command_hash = (
                compile_lookup.command_hash
                if compile_lookup is not None and compile_lookup.matched and compile_lookup.command_hash is not None
                else global_compile_command_hash
            )
            entries.append(
                SourceInventoryEntry(
                    source_id=source_id,
                    rel_path=rel,
                    source_kind=_source_kind(source),
                    sha256=_file_sha256(source),
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    compile_command_hash=compile_command_hash if source.suffix.lower() == ".c" else None,
                    toolchain_hash=toolchain_hash,
                    included_by=sorted(set(included_by[source_id])),
                    includes=includes_by_rel.get(rel, []),
                )
            )
        return entries

    def _global_compile_command_hash(self) -> str:
        return _hash_text(
            json.dumps(
                {"clang_args": self.config.clang_args},
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _load_ast_json_for_test(
        self,
        path: Path,
        rel_source: str,
        compile_lookup: _CompileCommandLookup,
    ) -> _AstLoadResult:
        started = time.perf_counter()
        executable = _resolve_executable(self.config.clang_executable, "clang", "clang_unavailable")
        command = [
            executable,
            *self.config.clang_args,
            *compile_lookup.flags,
            "-ferror-limit=0",
            "-Xclang",
            "-ast-dump=json",
            "-Xclang",
            "-detailed-preprocessing-record",
            "-fsyntax-only",
            str(path),
        ]
        cwd = compile_lookup.entry.directory_path if compile_lookup.matched and compile_lookup.entry is not None else None
        timeout_seconds = _ast_command_timeout_seconds(path)
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
                cwd=str(cwd) if cwd is not None else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise _RecoverableExtractError(
                "clang_ast_failed",
                "clang AST invocation timed out",
                diagnostic_kind="timeout",
                diagnostic_reason="timeout",
                details={"timeout_seconds": timeout_seconds},
            ) from exc
        except OSError as exc:
            raise _RecoverableExtractError(
                "clang_ast_failed",
                "clang AST invocation failed",
                diagnostic_kind="unknown",
                diagnostic_reason="subprocess_os_error",
            ) from exc
        if completed.returncode != 0 and not completed.stdout.strip():
            raise _RecoverableExtractError(
                "clang_ast_failed",
                "clang AST invocation failed",
                diagnostic_kind="fatal",
                diagnostic_reason="nonzero_exit",
            )
        try:
            ast = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise _RecoverableExtractError(
                "clang_ast_failed",
                "clang AST output must be valid JSON",
                diagnostic_kind="malformed_ast",
            ) from exc
        if not isinstance(ast, dict):
            raise _RecoverableExtractError(
                "clang_ast_failed",
                "clang AST root must be a JSON object",
                diagnostic_kind="malformed_ast",
            )
        if ast.get("kind") != "TranslationUnitDecl":
            raise _RecoverableExtractError(
                "clang_ast_failed",
                "clang AST root must be a TranslationUnitDecl",
                diagnostic_kind="malformed_ast",
            )
        inner = ast.get("inner")
        if not isinstance(inner, list) or not inner:
            raise _RecoverableExtractError(
                "clang_ast_failed",
                "clang AST TranslationUnitDecl must contain nodes",
                diagnostic_kind="malformed_ast",
            )
        if completed.returncode != 0 or _stderr_has_clang_error(completed.stderr):
            return _AstLoadResult(
                ast=ast,
                diagnostic_kind="partial_ast",
                diagnostic_reason=_partial_ast_reason(completed.returncode, completed.stderr),
                partial=True,
                warning_code="clang_ast_partial",
                backend="libclang",
                parse_duration_ms=_elapsed_ms(started),
            )
        return _AstLoadResult(ast=ast, backend="libclang", parse_duration_ms=_elapsed_ms(started))

    def _emit_file_event(
        self,
        rel_source: str,
        facts: List[CodeFact],
        relatives: List[FactRelative],
        stats: _FileMapStats,
        profile: str,
        started: float,
        compile_lookup: _CompileCommandLookup,
        ast_diagnostic_reason: str = "ok",
        backend: str = "libclang",
        parse_duration_ms: float = 0.0,
        traverse_duration_ms: float = 0.0,
        fact_count: Optional[int] = None,
        relative_count: Optional[int] = None,
        conditional_relative_count: Optional[int] = None,
        fact_kind_counts: Optional[Dict[str, int]] = None,
        relation_kind_counts: Optional[Dict[str, int]] = None,
        condition_kind_count: Optional[int] = None,
        relative_map_input_count: Optional[int] = None,
        relative_map_written_count: Optional[int] = None,
        relative_map_skipped_exact_count: Optional[int] = None,
    ) -> None:
        counts = Counter(fact.fact_kind for fact in facts) if fact_kind_counts is None else Counter(fact_kind_counts)
        relation_counts = (
            Counter(relative.relation_kind for relative in relatives)
            if relation_kind_counts is None
            else Counter(relation_kind_counts)
        )
        condition_counts = Counter(relative.condition.kind for relative in relatives if relative.condition is not None)
        fact_total = len(facts) if fact_count is None else fact_count
        relative_total = len(relatives) if relative_count is None else relative_count
        relative_map_input_total = relative_total if relative_map_input_count is None else relative_map_input_count
        relative_map_written_total = relative_total if relative_map_written_count is None else relative_map_written_count
        relative_map_skipped_exact_total = 0 if relative_map_skipped_exact_count is None else relative_map_skipped_exact_count
        conditional_total = (
            sum(1 for relative in relatives if relative.condition is not None)
            if conditional_relative_count is None
            else conditional_relative_count
        )
        condition_kind_total = len(condition_counts) if condition_kind_count is None else condition_kind_count
        is_warning = stats.warning_count > 0
        status = "warning" if is_warning else "ok"
        error_code = "clang_ast_partial" if stats.partial_ast_count > 0 else None
        outcome = "extracted_partial" if stats.partial_ast_count > 0 else "extracted"
        payload: Dict[str, JSONValue] = {
            "operation": "extract_file",
            "outcome": outcome,
            "source_kind": Path(rel_source).suffix.lower().lstrip("."),
            "profile": profile,
            "backend": backend,
            "parse_duration_ms": round(parse_duration_ms, 3),
            "traverse_duration_ms": round(traverse_duration_ms, 3),
            "function_count": counts.get("function", 0),
            "relation_kind_count": len(relation_counts),
            "condition_kind_count": condition_kind_total,
        }
        if stats.partial_ast_count > 0:
            payload["error_code"] = "clang_ast_partial"
            payload["diagnostic_kind"] = "partial_ast"
            payload["diagnostic_reason"] = ast_diagnostic_reason
            payload["partial_ast_count"] = stats.partial_ast_count
        event_counts = {
            "fact_count": fact_total,
            "relative_count": relative_total,
            "relative_map_input_count": relative_map_input_total,
            "relative_map_written_count": relative_map_written_total,
            "relative_map_skipped_exact_count": relative_map_skipped_exact_total,
            "conditional_relative_count": conditional_total,
            "field_read_count": relation_counts.get("field_read", 0),
            "field_write_count": relation_counts.get("field_write", 0),
            "typed_member_expr_count": stats.typed_member_expr_count,
            "typed_call_expr_count": stats.typed_call_expr_count,
            "source_from_loc_file_count": stats.source_from_loc_file_count,
            "source_fallback_count": stats.source_fallback_count,
            "unresolved_call_count": stats.unresolved_call_count,
            "field_owner_count": stats.field_owner_count,
            "record_owner_count": stats.record_owner_count,
            "anonymous_record_count": stats.anonymous_record_count,
            "synthetic_type_fact_count": stats.synthetic_type_fact_count,
            "field_decl_count": stats.field_decl_count,
            "field_fact_count": stats.field_fact_count,
            "field_decl_without_fact_count": stats.field_decl_without_fact_count,
            "wrapped_member_expr_count": stats.wrapped_member_expr_count,
            "macro_wrapped_member_expr_count": stats.macro_wrapped_member_expr_count,
            "bitwise_member_expr_count": stats.bitwise_member_expr_count,
            "compound_field_access_count": stats.compound_field_access_count,
            "field_access_scan_truncated_count": stats.field_access_scan_truncated_count,
            "field_access_resolved_count": stats.field_access_resolved_count,
            "field_access_unresolved_count": stats.field_access_unresolved_count,
            "function_pointer_slot_count": stats.function_pointer_slot_count,
            "function_pointer_assignment_count": stats.function_pointer_assignment_count,
            "function_pointer_dispatch_count": stats.function_pointer_dispatch_count,
            "macro_direct_call_count": stats.macro_direct_call_count,
            "unresolved_dispatch_slot_count": stats.unresolved_dispatch_slot_count,
            "unresolved_dispatch_function_count": stats.unresolved_dispatch_function_count,
            "header_decl_cache_hit_count": stats.header_decl_cache_hit_count,
            "header_decl_cache_miss_count": stats.header_decl_cache_miss_count,
            "header_decl_skipped_subtree_count": stats.header_decl_skipped_subtree_count,
            "header_decl_seed_count": stats.header_decl_seed_count,
            "compile_command_hit_count": 1 if compile_lookup.matched else 0,
            "compile_command_miss_count": 1 if compile_lookup.configured and not compile_lookup.matched else 0,
            "compile_command_argument_count": compile_lookup.argument_count,
            "compile_command_stripped_argument_count": compile_lookup.stripped_argument_count,
            "partial_ast_count": stats.partial_ast_count,
            "warning_count": stats.warning_count,
        }
        event = LogEvent(
            event_name="extractor.code.file",
            channel="initializer",
            status=status,
            duration_ms=_elapsed_ms(started),
            subject_id=_hash_text(rel_source)[:16],
            summary=f"{outcome} {fact_total} facts and {relative_total} relatives from {rel_source}",
            counts=event_counts,
            error_code=error_code,
            payload=payload,
        )
        self._emit_progress_event("file_done", started=started, source=rel_source, counts=event_counts, payload=payload)
        if self.log_enabled:
            self._write_event(event)

    def _emit_file_error(self, rel_source: str, code: str, profile: str, started: float) -> None:
        counts = {"fact_count": 0, "warning_count": 1}
        payload = {
            "operation": "extract_file",
            "outcome": "failed",
            "error_code": code,
            "source_kind": Path(rel_source).suffix.lower().lstrip("."),
            "profile": profile,
        }
        event = LogEvent(
            event_name="extractor.code.error",
            channel="initializer",
            status="error",
            duration_ms=_elapsed_ms(started),
            subject_id=_hash_text(rel_source)[:16],
            summary=f"failed to extract {rel_source}: {code}",
            counts=counts,
            error_code=code,
            payload=payload,
        )
        self._emit_progress_event("file_done", started=started, source=rel_source, counts=counts, payload=payload)
        if self.log_enabled:
            self._write_event(event)

    def _emit_file_warning(
        self,
        rel_source: str,
        code: str,
        profile: str,
        started: float,
        diagnostic_kind: str,
        compile_lookup: _CompileCommandLookup,
        diagnostic_reason: Optional[str] = None,
        diagnostic_details: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        payload: Dict[str, JSONValue] = {
            "operation": "extract_file",
            "outcome": "skipped",
            "error_code": code,
            "diagnostic_kind": diagnostic_kind,
            "source_kind": Path(rel_source).suffix.lower().lstrip("."),
            "profile": profile,
            "backend": self.toolchain_probe_result.backend if self.toolchain_probe_result is not None else "libclang",
        }
        if diagnostic_reason is not None:
            payload["diagnostic_reason"] = diagnostic_reason
        if diagnostic_details:
            payload.update(diagnostic_details)
        counts = {
            "fact_count": 0,
            "relative_count": 0,
            "conditional_relative_count": 0,
            "field_read_count": 0,
            "field_write_count": 0,
            "typed_member_expr_count": 0,
            "typed_call_expr_count": 0,
            "source_from_loc_file_count": 0,
            "source_fallback_count": 0,
            "unresolved_call_count": 0,
            "field_owner_count": 0,
            "record_owner_count": 0,
            "anonymous_record_count": 0,
            "synthetic_type_fact_count": 0,
            "field_decl_count": 0,
            "field_fact_count": 0,
            "field_decl_without_fact_count": 0,
            "wrapped_member_expr_count": 0,
            "macro_wrapped_member_expr_count": 0,
            "bitwise_member_expr_count": 0,
            "compound_field_access_count": 0,
            "field_access_scan_truncated_count": 0,
            "field_access_resolved_count": 0,
            "field_access_unresolved_count": 0,
            "function_pointer_slot_count": 0,
            "function_pointer_assignment_count": 0,
            "function_pointer_dispatch_count": 0,
            "macro_direct_call_count": 0,
            "unresolved_dispatch_slot_count": 0,
            "unresolved_dispatch_function_count": 0,
            "compile_command_hit_count": 1 if compile_lookup.matched else 0,
            "compile_command_miss_count": 1 if compile_lookup.configured and not compile_lookup.matched else 0,
            "compile_command_argument_count": compile_lookup.argument_count,
            "compile_command_stripped_argument_count": compile_lookup.stripped_argument_count,
            "partial_ast_count": 0,
            "warning_count": 1,
        }
        event = LogEvent(
            event_name="extractor.code.file",
            channel="initializer",
            status="warning",
            duration_ms=_elapsed_ms(started),
            subject_id=_hash_text(rel_source)[:16],
            summary=f"skipped {rel_source}: {code}",
            counts=counts,
            error_code=code,
            payload=payload,
        )
        self._emit_progress_event("file_done", started=started, source=rel_source, counts=counts, payload=payload)
        if self.log_enabled:
            self._write_event(event)

    def _emit_compile_database_event(
        self,
        stats: _CompileCommandStats,
        status: str,
        started: float,
        error_code: Optional[str],
    ) -> None:
        counts = {
            "compile_command_entry_count": stats.entry_count,
            "compile_command_indexed_source_count": stats.indexed_source_count,
            "compile_command_duplicate_source_count": stats.duplicate_source_count,
            "compile_command_ignored_outside_repo_count": stats.ignored_outside_repo_count,
            "compile_command_stripped_argument_count": stats.stripped_argument_count,
        }
        payload = {
            "operation": "compile_database_index",
            "outcome": "indexed" if error_code is None else "failed",
            "error_code": error_code,
            "compile_command_entry_count": stats.entry_count,
            "compile_command_indexed_source_count": stats.indexed_source_count,
            "compile_command_duplicate_source_count": stats.duplicate_source_count,
            "compile_command_ignored_outside_repo_count": stats.ignored_outside_repo_count,
            "compile_command_stripped_argument_count": stats.stripped_argument_count,
        }
        event = LogEvent(
            event_name="extractor.code.compile_database",
            channel="initializer",
            status=status,
            duration_ms=_elapsed_ms(started),
            error_code=error_code,
            summary="compile database indexed" if error_code is None else "compile database malformed",
            counts=counts,
            payload=payload,
        )
        self._emit_progress_event("compile_database", started=started, counts=counts, payload=payload)
        if self.log_enabled:
            self._write_event(event)

    def _emit_toolchain_event(
        self,
        *,
        status: str,
        started: float,
        error_code: Optional[str],
        result: Optional[ToolchainProbeResult],
        missing_evidence: Sequence[str] = (),
    ) -> None:
        payload = {
            "operation": "toolchain_probe",
            "outcome": "failed" if error_code else "probed",
            "backend": "libclang" if result is None else result.backend,
            "ast_json_supported": False if result is None else result.ast_json_supported,
            "type_driven_ast": False if result is None else result.type_driven_ast,
            "loc_file_supported": False if result is None else result.loc_file_supported,
            "call_reference_supported": False if result is None else result.call_reference_supported,
            "member_reference_supported": False if result is None else result.member_reference_supported,
            "qual_type_supported": False if result is None else result.qual_type_supported,
            "gcc_required": False if result is None else result.gcc_required,
            "gcc_checked": False if result is None else result.gcc_checked,
        }
        counts = {"warning_count": 0 if result is None else len(result.warning_codes)}
        if result is not None:
            payload.update(
                {
                    "clang_vendor": result.clang_vendor,
                    "clang_version": result.clang_version,
                    "ast_root_kind": result.ast_root_kind,
                    "libclang_version": result.libclang_version,
                    "libclang_library_scope": result.libclang_library_scope,
                    "version_match": result.version_match,
                }
            )
        if error_code is not None:
            payload["error_code"] = error_code
        if missing_evidence:
            payload["missing_evidence"] = ",".join(missing_evidence)
        event = LogEvent(
            event_name="extractor.code.toolchain",
            channel="initializer",
            status=status,
            duration_ms=_elapsed_ms(started),
            summary="clang capability probe failed" if error_code else "clang capability probe passed",
            counts=counts,
            error_code=error_code,
            payload=payload,
        )
        self._emit_progress_event("toolchain", started=started, counts=counts, payload=payload)
        if self.log_enabled:
            self._write_event(event)

    def toolchain_probe_result_to_digest(self) -> Optional[Dict[str, JSONValue]]:
        result = self.toolchain_probe_result
        if result is None:
            return None
        return {
            "clang_vendor": result.clang_vendor,
            "clang_version": result.clang_version,
            "ast_json_supported": result.ast_json_supported,
            "type_driven_ast": result.type_driven_ast,
            "loc_file_supported": result.loc_file_supported,
            "call_reference_supported": result.call_reference_supported,
            "member_reference_supported": result.member_reference_supported,
            "qual_type_supported": result.qual_type_supported,
            "gcc_required": result.gcc_required,
            "gcc_checked": result.gcc_checked,
            "backend": result.backend,
            "libclang_version": result.libclang_version,
            "libclang_library_scope": result.libclang_library_scope,
            "version_match": result.version_match,
            "warning_codes": list(result.warning_codes),
        }

    def _emit_direct_call_resolution_event(self, stats: _DirectCallResolutionStats, profile: str) -> None:
        if not self.log_enabled:
            return
        status = "warning" if stats.has_warning() else "ok"
        counts = stats.to_counts()
        self._write_event(
            LogEvent(
                event_name="extractor.code.direct_call_resolution",
                channel="initializer",
                status=status,
                summary=f"resolved {stats.resolved_call_count} of {stats.pending_call_count} pending direct calls",
                counts=counts,
                payload={
                    "operation": "resolve_pending_direct_calls",
                    "outcome": "warning" if stats.has_warning() else "resolved",
                    "profile": profile,
                    **counts,
                },
            )
        )

    def _emit_relative_merge_event(self, stats: _RelativeExternalMergeStats, profile: str) -> None:
        if not self.log_enabled:
            return
        status = "error" if stats.conflict_count else "ok"
        counts = stats.to_counts()
        self._write_event(
            LogEvent(
                event_name="extractor.code.relative_merge",
                channel="initializer",
                status=status,
                duration_ms=stats.duration_ms,
                summary=f"merged {stats.accepted_count} of {stats.input_count} relative segment lines",
                counts=counts,
                error_code="map_reduce_conflict" if stats.conflict_count else None,
                payload={
                    "operation": "external_relative_merge",
                    "outcome": "conflict" if stats.conflict_count else "merged",
                    "mode": "external_k_way",
                    "profile": profile,
                    **counts,
                },
            )
        )

    def _emit_worker_pool_event(
        self,
        *,
        source_count: int,
        worker_count: int,
        max_unmerged: int,
        successful_file_count: int,
        skipped_file_count: int,
        partial_ast_count: int,
        warning_count: int,
        profile: str,
        started: float,
        header_decl_cache_entry_count: int = 0,
        map_output_segment_count: int = 0,
        map_output_bytes: int = 0,
        stale_run_gc_count: int = 0,
        relative_map_input_count: int = 0,
        relative_map_written_count: int = 0,
        relative_map_skipped_exact_count: int = 0,
        relative_worker_duplicate_exact_count: int = 0,
        relative_worker_duplicate_conflict_count: int = 0,
        relative_worker_dedup_tracked_entry_count: int = 0,
        relative_worker_dedup_saturated_count: int = 0,
        fact_line_passthrough_count: int = 0,
        relative_line_passthrough_count: int = 0,
        fact_line_passthrough_bytes: int = 0,
        relative_line_passthrough_bytes: int = 0,
        fact_line_reencoded_count: int = 0,
        relative_line_reencoded_count: int = 0,
        fact_duplicate_exact_count: int = 0,
        fact_duplicate_merge_parse_count: int = 0,
        fact_duplicate_conflict_count: int = 0,
        relative_duplicate_exact_count: int = 0,
        relative_duplicate_conflict_count: int = 0,
    ) -> None:
        status = "warning" if skipped_file_count > 0 or partial_ast_count > 0 or warning_count > 0 else "ok"
        mode = "serial" if worker_count <= 1 else "bounded_pool"
        counts = {
            "source_count": source_count,
            "worker_count": worker_count,
            "successful_file_count": successful_file_count,
            "skipped_file_count": skipped_file_count,
            "partial_ast_count": partial_ast_count,
            "warning_count": warning_count,
            "header_decl_cache_entry_count": header_decl_cache_entry_count,
            "map_output_segment_count": map_output_segment_count,
            "map_output_bytes": map_output_bytes,
            "stale_run_gc_count": stale_run_gc_count,
            "relative_map_input_count": relative_map_input_count,
            "relative_map_written_count": relative_map_written_count,
            "relative_map_skipped_exact_count": relative_map_skipped_exact_count,
            "relative_worker_duplicate_exact_count": relative_worker_duplicate_exact_count,
            "relative_worker_duplicate_conflict_count": relative_worker_duplicate_conflict_count,
            "relative_worker_dedup_tracked_entry_count": relative_worker_dedup_tracked_entry_count,
            "relative_worker_dedup_saturated_count": relative_worker_dedup_saturated_count,
            "fact_line_passthrough_count": fact_line_passthrough_count,
            "relative_line_passthrough_count": relative_line_passthrough_count,
            "fact_line_passthrough_bytes": fact_line_passthrough_bytes,
            "relative_line_passthrough_bytes": relative_line_passthrough_bytes,
            "fact_line_reencoded_count": fact_line_reencoded_count,
            "relative_line_reencoded_count": relative_line_reencoded_count,
            "fact_duplicate_exact_count": fact_duplicate_exact_count,
            "fact_duplicate_merge_parse_count": fact_duplicate_merge_parse_count,
            "fact_duplicate_conflict_count": fact_duplicate_conflict_count,
            "relative_duplicate_exact_count": relative_duplicate_exact_count,
            "relative_duplicate_conflict_count": relative_duplicate_conflict_count,
            "passthrough_ratio_percent": _passthrough_ratio_percent(
                fact_line_passthrough_count + relative_line_passthrough_count,
                fact_line_reencoded_count + relative_line_reencoded_count,
            ),
        }
        payload: Dict[str, JSONValue] = {
            "operation": "parallel_extract",
            "outcome": "warning" if status == "warning" else "completed",
            "mode": mode,
            "profile": profile,
            "max_unmerged": max_unmerged,
            **counts,
        }
        event = LogEvent(
            event_name="extractor.code.worker_pool",
            channel="initializer",
            status=status,
            duration_ms=_elapsed_ms(started),
            summary=f"{mode} extracted {successful_file_count} of {source_count} sources",
            counts=counts,
            payload=payload,
        )
        if self.log_enabled:
            self._write_event(event)

    def _write_event(self, event: LogEvent) -> None:
        try:
            open_log(self.target_repo).write_event(event)
        except LogError:
            pass

    def _emit_progress_event(
        self,
        kind: str,
        *,
        started: float,
        source: Optional[str] = None,
        total: Optional[int] = None,
        counts: Optional[Dict[str, int]] = None,
        payload: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        if self.progress_sink is None:
            return
        event = InitProgressEvent(
            kind=kind,
            elapsed_ms=_elapsed_ms(started),
            source=source,
            total=total,
            counts=dict(counts or {}),
            payload=dict(payload or {}),
        )
        try:
            self.progress_sink(event)
        except Exception:
            self.progress_sink = None


# Late import avoids a cycle while preserving the original global name used by CodeFactExtractor.stream().
from .streaming import _StreamingExtraction  # noqa: E402
from .streaming import _passthrough_ratio_percent  # noqa: E402
from .streaming import _ast_command_timeout_seconds  # noqa: E402
from .streaming import _stderr_has_clang_error  # noqa: E402
from .streaming import _partial_ast_reason  # noqa: E402

__all__ = [name for name in globals() if not name.startswith("__")]

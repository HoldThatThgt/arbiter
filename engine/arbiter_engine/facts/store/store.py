from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import threading
import uuid
from collections import Counter, OrderedDict
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from ._common import JSONValue
from ._common import LogError, LogEvent, open_log

from .constants import *
from .models import *
from .views import *
from .search import *
from .serialization import *
from .read_index import *
from .utils import *
from .snapshot_reader import *
from .snapshot_writer import *
from .store_events import *

from ..relocation import facts_dir


class FileFactStore:
    def __init__(self, target_repo: Path, mode: str = "r", *, log_enabled: bool = True) -> None:
        if mode not in {"r", "w"}:
            raise StorageError("invalid_mode", "mode must be 'r' or 'w'")
        self.target_repo = Path(target_repo)
        self.cipher_dir = facts_dir(self.target_repo)  # M4: arbiter store root (was cipher-2 ".cipher")
        self.mode = mode
        self.log_enabled = log_enabled
        self._log_write_failures = 0
        self._latest_log_error_code: Optional[str] = None

    def replace_facts(self, facts: Iterable[FactRecord]) -> StorageManifest:
        return self._replace_snapshot(facts, [], [], operation="replace_facts")

    def replace_snapshot(
        self,
        facts: Iterable[FactRecord],
        relatives: Iterable[FactRelative],
        source_inventory: Optional[Iterable[SourceInventoryEntry]] = None,
    ) -> StorageManifest:
        return self._replace_snapshot(facts, relatives, source_inventory or [], operation="replace_snapshot")

    def replace_snapshot_sorted_unique(
        self,
        facts: Iterable[FactRecord],
        relatives: Iterable[FactRelative],
        source_inventory: Optional[Iterable[SourceInventoryEntry]] = None,
    ) -> StorageManifest:
        return self._replace_snapshot(
            facts,
            relatives,
            source_inventory or [],
            operation="replace_snapshot_sorted_unique",
            sorted_unique=True,
        )

    def _replace_snapshot_preencoded_sorted_unique(
        self,
        facts: Iterable[EncodedFactLine],
        relatives: Iterable[EncodedRelativeLine],
        source_inventory: Optional[Iterable[SourceInventoryEntry]] = None,
    ) -> StorageManifest:
        return self._replace_snapshot(
            facts,
            relatives,
            source_inventory or [],
            operation="replace_snapshot_preencoded_sorted_unique",
            preencoded_sorted_unique=True,
        )

    def _replace_snapshot(
        self,
        facts: Iterable[Any],
        relatives: Iterable[Any],
        source_inventory: Iterable[SourceInventoryEntry],
        *,
        operation: str,
        sorted_unique: bool = False,
        preencoded_sorted_unique: bool = False,
    ) -> StorageManifest:
        started = _now()
        if self.mode != "w":
            raise StorageError("read_only", f"{operation} requires mode='w'")
        self._ensure_cipher_path_safe()
        lock_dir = self._acquire_lock(operation)
        try:
            staging_dir = self._safe_path("staging", f"storage-{os.getpid()}-{uuid.uuid4().hex}")
            staging_dir.parent.mkdir(parents=True, exist_ok=True)
            staging_dir.mkdir()
            try:
                if preencoded_sorted_unique:
                    prepared = self._prepare_preencoded_sorted_unique_snapshot_staging(
                        staging_dir,
                        facts,
                        relatives,
                        source_inventory,
                    )
                elif sorted_unique:
                    prepared = self._prepare_sorted_unique_snapshot_staging(
                        staging_dir,
                        facts,
                        relatives,
                        source_inventory,
                    )
                else:
                    prepared = self._prepare_snapshot_staging(staging_dir, facts, relatives, source_inventory)
            except Exception:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                raise
            facts_sha256 = prepared["facts_sha256"]
            relatives_sha256 = prepared["relatives_sha256"]
            source_inventory_sha256 = prepared["source_inventory_sha256"]
            content_sha256 = _sha256_text(
                f"{facts_sha256}\n{relatives_sha256}\n{source_inventory_sha256}\n"
            )
            snapshot_id = "sha256-" + content_sha256[:16]
            read_index_started = _now()
            read_index = self._build_read_index_file(staging_dir, snapshot_id, prepared)
            read_index_build_ms = round(_elapsed_ms(read_index_started))
            snapshots_dir = self._safe_path("snapshots")
            snapshot_dir = self._safe_path("snapshots", snapshot_id)
            existing_manifest = self._read_manifest_if_exists(snapshot_dir)
            if existing_manifest is not None and (
                existing_manifest.facts_sha256 != facts_sha256
                or existing_manifest.relatives_sha256 != relatives_sha256
                or existing_manifest.source_inventory_sha256 != source_inventory_sha256
            ):
                raise StorageError("snapshot_id_collision", "snapshot id prefix collision", path=snapshot_dir)

            created_at = existing_manifest.created_at if existing_manifest is not None else _now()
            reused = existing_manifest is not None
            stats = self._build_stats(
                fact_count=prepared["fact_count"],
                relative_count=prepared["relative_count"],
                source_count=prepared["source_count"],
                fact_kinds=prepared["fact_kinds"],
                relation_kinds=prepared["relation_kinds"],
                conditional_relative_count=prepared["conditional_relative_count"],
                orphan_relative_count=0,
                profiles=prepared["profiles"],
                source_files=prepared["source_files"],
                with_caller_count=prepared["with_caller_count"],
                with_callee_count=prepared["with_callee_count"],
                snapshot_id=snapshot_id,
                created_at=created_at,
                log_write_failures=existing_manifest.log_write_failures if existing_manifest is not None else 0,
                latest_log_error_code=existing_manifest.latest_log_error_code if existing_manifest is not None else None,
                bytes_on_disk=0,
                bytes_on_disk_total=0,
                uncompressed_bytes=prepared["uncompressed_bytes"],
                compressed_data_bytes=prepared["compressed_data_bytes"],
                file_bytes=prepared["file_bytes"],
                read_index=read_index,
                extra_snapshot_id=None if reused else snapshot_id,
            )
            manifest = StorageManifest(
                schema_version=SCHEMA_VERSION,
                snapshot_id=snapshot_id,
                snapshot_format=SNAPSHOT_FORMAT,
                compression=SNAPSHOT_COMPRESSION,
                reused=False,
                created_at=created_at,
                fact_count=prepared["fact_count"],
                relative_count=prepared["relative_count"],
                source_count=prepared["source_count"],
                facts_sha256=facts_sha256,
                relatives_sha256=relatives_sha256,
                source_inventory_sha256=source_inventory_sha256,
                bytes_on_disk=0,
                uncompressed_bytes=prepared["uncompressed_bytes"],
                compressed_data_bytes=prepared["compressed_data_bytes"],
                compression_ratio=_compression_ratio(prepared["compressed_data_bytes"], prepared["uncompressed_bytes"]),
                storage_overhead_ratio=1.0,
                file_bytes=prepared["file_bytes"],
                read_index=read_index,
                stats=stats.to_json(),
                log_write_failures=stats.log_write_failures,
                latest_log_error_code=stats.latest_log_error_code,
            )

            if not reused:
                try:
                    manifest = self._write_manifest_and_stats(staging_dir, manifest)
                    snapshots_dir.mkdir(parents=True, exist_ok=True)
                    os.replace(staging_dir, snapshot_dir)
                finally:
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir)
            else:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                manifest = replace(existing_manifest, reused=True)

            self._write_current(snapshots_dir, snapshot_id)
            outcome = "skipped_idempotent" if reused else "created"
            failures, latest = self._emit_event(
                "storage.write",
                status="ok",
                operation=operation,
                outcome=outcome,
                started=started,
                snapshot_id=snapshot_id,
                fact_count=prepared["fact_count"],
                relative_count=prepared["relative_count"],
                source_count=prepared["source_count"],
                conditional_relative_count=prepared["conditional_relative_count"],
                relation_kind_count=len(prepared["relation_kinds"]),
                snapshot_format=manifest.snapshot_format,
                compression=manifest.compression,
                bytes_written=manifest.bytes_on_disk,
                uncompressed_bytes=manifest.uncompressed_bytes,
                compressed_data_bytes=manifest.compressed_data_bytes,
                compression_ratio_percent=_compression_ratio_percent(
                    manifest.compressed_data_bytes,
                    manifest.uncompressed_bytes,
                ),
                storage_overhead_ratio_percent=_compression_ratio_percent(
                    manifest.bytes_on_disk,
                    manifest.uncompressed_bytes,
                ),
                read_index_format=READ_INDEX_FORMAT,
                read_index_codec=READ_INDEX_PAYLOAD_CODEC,
                read_index_bytes=int(manifest.read_index["bytes_on_disk"]),
                read_index_build_ms=read_index_build_ms,
                facts_raw_bytes=int(manifest.file_bytes["facts"]["raw_bytes"]),
                facts_compressed_bytes=int(manifest.file_bytes["facts"]["compressed_bytes"]),
                relatives_raw_bytes=int(manifest.file_bytes["relatives"]["raw_bytes"]),
                relatives_compressed_bytes=int(manifest.file_bytes["relatives"]["compressed_bytes"]),
                source_inventory_raw_bytes=int(manifest.file_bytes["source_inventory"]["raw_bytes"]),
                source_inventory_compressed_bytes=int(manifest.file_bytes["source_inventory"]["compressed_bytes"]),
            )
            if failures:
                manifest = self._persist_log_degradation(snapshot_dir, manifest, failures, latest)
            return replace(manifest, reused=reused)
        except StorageError as exc:
            self._emit_storage_error(exc, operation, started)
            raise
        finally:
            self._release_lock(lock_dir)

    def iter_facts(self) -> Iterator[FactRecord]:
        started = _now()
        records = list(self._stream_current_facts())
        self._emit_event(
            "storage.read",
            status="ok",
            operation="iter_facts",
            outcome="read",
            started=started,
            fact_count=len(records),
        )
        return iter(records)

    def iter_relatives(self) -> Iterator[FactRelative]:
        started = _now()
        records = list(self._stream_current_relatives())
        self._emit_event(
            "storage.read",
            status="ok",
            operation="iter_relatives",
            outcome="read",
            started=started,
            relative_count=len(records),
        )
        return iter(records)

    def iter_source_inventory(self) -> Iterator[SourceInventoryEntry]:
        started = _now()
        records = list(self._stream_current_source_inventory())
        self._emit_event(
            "storage.read",
            status="ok",
            operation="iter_source_inventory",
            outcome="read",
            started=started,
            source_count=len(records),
        )
        return iter(records)

    def open_view(self, overlay: Optional[TemporaryOverlay] = None) -> FactView:
        return FactView(self, overlay)

    def get_fact(self, object_id: str) -> Optional[FactRecord]:
        if not isinstance(object_id, str) or not object_id:
            raise StorageError("invalid_fact_id", "object_id must be a non-empty string")
        started = _now()
        index = self._read_index()
        fact = index.get_fact(object_id) if index is not None else None
        if fact is not None:
            self._emit_event(
                "storage.read",
                status="ok",
                operation="get_fact",
                outcome="found",
                started=started,
                fact_count=1,
            )
            return fact
        self._emit_event(
            "storage.read",
            status="ok",
            operation="get_fact",
            outcome="missing",
            started=started,
            fact_count=0,
        )
        return None

    def search(self, query: str, limit: int = 20) -> List[FactRecord]:
        started = _now()
        if not isinstance(query, str):
            error = StorageError("invalid_query", "query must be a string")
            self._emit_storage_error(error, "search", started)
            raise error
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            error = StorageError("invalid_limit", "limit must be >= 1")
            self._emit_storage_error(error, "search", started)
            raise error
        index = self._read_index()
        terms = _search_terms(query)
        if not terms:
            results = index.search(query, limit) if index is not None else []
            query_kind = "empty"
        else:
            results = index.search(query, limit) if index is not None else []
            query_kind = "terms"
        self._emit_event(
            "storage.search",
            status="ok",
            operation="search",
            outcome="searched",
            started=started,
            matched_count=len(results),
            limit=limit,
            term_count=len(terms),
            query_kind=query_kind,
            query_preview=query[:80],
        )
        return results

    def relation_search(self, query: str, limit: int = 20) -> RelationSearchResult:
        started = _now()
        if not isinstance(query, str):
            error = StorageError("invalid_query", "query must be a string")
            self._emit_storage_error(error, "search", started)
            raise error
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            error = StorageError("invalid_limit", "limit must be >= 1")
            self._emit_storage_error(error, "search", started)
            raise error
        try:
            index = self._read_index()
            if index is None:
                result = _relation_search_from_records(query, [], [], limit)
            else:
                result = index.relation_search(query, limit)
        except StorageError as exc:
            self._emit_storage_error(exc, "search", started)
            raise
        self._emit_relation_search_event(result, query, limit, started)
        return result

    def relatives_for_fact(
        self,
        fact_id: str,
        direction: str = "both",
        relation_kind: Optional[str] = None,
        limit: int = 20,
    ) -> List[FactRelative]:
        started = _now()
        if not isinstance(fact_id, str) or not fact_id:
            error = StorageError("invalid_fact_id", "fact_id must be a non-empty string")
            self._emit_storage_error(error, "relations", started)
            raise error
        if direction not in {"incoming", "outgoing", "both"}:
            error = StorageError("invalid_direction", "direction must be incoming, outgoing, or both")
            self._emit_storage_error(error, "relations", started)
            raise error
        if relation_kind is not None and relation_kind not in RELATION_KINDS:
            error = StorageError("invalid_relation_kind", unsupported_relation_kind_message(relation_kind))
            self._emit_storage_error(error, "relations", started)
            raise error
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
            error = StorageError("invalid_limit", "limit must be between 1 and 100")
            self._emit_storage_error(error, "relations", started)
            raise error
        index = self._read_index()
        if index is None:
            results: List[FactRelative] = []
            truncated_count = 0
        else:
            results, truncated_count = index.relatives_for_fact(fact_id, direction, relation_kind, limit)
        self._emit_event(
            "storage.relations",
            status="ok",
            operation="relations",
            outcome="searched",
            started=started,
            relative_count=len(results),
            truncated_count=truncated_count,
            limit=limit,
            direction=direction,
            relation_kind=relation_kind,
        )
        return results

    def count_relatives_for_fact(
        self,
        fact_id: str,
        direction: str = "both",
        relation_kind: Optional[str] = None,
    ) -> int:
        _validate_relative_query(fact_id, direction, relation_kind)
        index = self._read_index()
        if index is None:
            return 0
        return index.count_relatives_for_fact(fact_id, direction, relation_kind)

    def stats(self) -> StorageStats:
        current = self._current_snapshot_dir()
        if current is None:
            return StorageStats(
                total_facts=0,
                total_relatives=0,
                total_sources=0,
                fact_kinds={},
                relation_kinds={},
                conditional_relative_count=0,
                orphan_relative_count=0,
                profiles={},
                source_files={},
                with_caller_count=0,
                with_callee_count=0,
                snapshot_count=self._snapshot_count(),
                bytes_on_disk=0,
                bytes_on_disk_total=self._all_snapshots_bytes(),
                uncompressed_bytes=0,
                compressed_data_bytes=0,
                compression_ratio=1.0,
                storage_overhead_ratio=1.0,
                file_bytes={},
                read_index_state="missing",
                read_index_bytes=0,
                read_index_schema_version=None,
                read_index_codec=None,
                snapshot_format=None,
                compression=None,
                snapshot_id=None,
                last_updated=None,
                log_write_failures=self._log_write_failures,
                latest_log_error_code=self._latest_log_error_code,
                lock_state=self._lock_state(),
            )
        manifest, stats = self._load_metadata(current, validate_actual_bytes=True)
        return replace(
            stats,
            log_write_failures=stats.log_write_failures + self._log_write_failures,
            latest_log_error_code=self._latest_log_error_code or stats.latest_log_error_code,
            lock_state=self._lock_state(),
        )



def open_fact_store(target_repo: Path, mode: str = "r", *, log_enabled: bool = True) -> FileFactStore:
    return FileFactStore(target_repo, mode=mode, log_enabled=log_enabled)


FileFactStore._stream_current_facts = _stream_current_facts
FileFactStore._stream_current_relatives = _stream_current_relatives
FileFactStore._stream_current_source_inventory = _stream_current_source_inventory
FileFactStore._stream_current_fact_lines = _stream_current_fact_lines
FileFactStore._stream_current_fact_raw_lines = _stream_current_fact_raw_lines
FileFactStore._stream_current_relative_lines = _stream_current_relative_lines
FileFactStore._stream_current_relative_raw_lines = _stream_current_relative_raw_lines
FileFactStore._stream_current_source_inventory_raw_lines = _stream_current_source_inventory_raw_lines
FileFactStore._read_index = _read_index
FileFactStore._open_persistent_read_index = _open_persistent_read_index
FileFactStore._validate_read_index_metadata = _validate_read_index_metadata
FileFactStore._load_metadata = _load_metadata
FileFactStore._assert_metadata_consistent = _assert_metadata_consistent
FileFactStore._assert_snapshot_file_sizes = _assert_snapshot_file_sizes
FileFactStore._read_manifest_if_exists = _read_manifest_if_exists
FileFactStore._current_snapshot_dir = _current_snapshot_dir
FileFactStore._build_read_index_file = _build_read_index_file
FileFactStore._prepare_snapshot_staging = _prepare_snapshot_staging
FileFactStore._prepare_sorted_unique_snapshot_staging = _prepare_sorted_unique_snapshot_staging
FileFactStore._prepare_preencoded_sorted_unique_snapshot_staging = _prepare_preencoded_sorted_unique_snapshot_staging
FileFactStore._build_stats = _build_stats
FileFactStore._write_manifest_and_stats = _write_manifest_and_stats
FileFactStore._write_current = _write_current
FileFactStore._persist_log_degradation = _persist_log_degradation
FileFactStore._emit_relation_search_event = _emit_relation_search_event
FileFactStore._emit_event = _emit_event
FileFactStore._emit_storage_error = _emit_storage_error
FileFactStore._ensure_cipher_path_safe = _ensure_cipher_path_safe
FileFactStore._safe_path = _safe_path
FileFactStore._acquire_lock = _acquire_lock
FileFactStore._release_lock = _release_lock
FileFactStore._lock_state = _lock_state
FileFactStore._snapshot_count = _snapshot_count
FileFactStore._all_snapshots_bytes = _all_snapshots_bytes

__all__ = [name for name in globals() if not name.startswith("__")]

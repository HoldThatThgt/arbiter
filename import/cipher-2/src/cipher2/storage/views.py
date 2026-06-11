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

from cipher2.common import JSONValue
from cipher2.tools.log import LogError, LogEvent, open_log

from .constants import *
from .models import *
from .search import *
from .utils import *

@dataclass(frozen=True)
class TemporaryOverlay:
    overlay_id: str
    view_state: str = "overlay"
    fact_upserts: List[FactRecord] = field(default_factory=list)
    relative_upserts: List[FactRelative] = field(default_factory=list)
    fact_tombstones: Set[str] = field(default_factory=set)
    relative_tombstones: Set[str] = field(default_factory=set)
    source_tombstones: Set[str] = field(default_factory=set)
    stale_source_count: int = 0
    pending_task_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.overlay_id, str) or not self.overlay_id:
            raise StorageError("invalid_overlay", "overlay_id must be a non-empty string")
        if self.view_state not in {"base", "stale", "pending", "overlay", "error"}:
            raise StorageError("invalid_overlay", "view_state is not supported")

class FactView:
    def __init__(self, store: "FileFactStore", overlay: Optional[TemporaryOverlay] = None) -> None:
        self._store = store
        self._overlay = overlay
        self.base_snapshot_id = store.stats().snapshot_id
        self.overlay_id = overlay.overlay_id if overlay is not None else None
        self.view_state = overlay.view_state if overlay is not None else "base"
        self.view_id = f"{self.base_snapshot_id or 'empty'}:{self.overlay_id or 'base'}"
        self._overlay_relatives_cache: Optional[List[FactRelative]] = None

    def get_fact(self, object_id: str) -> Optional[FactRecord]:
        if self._overlay is None or self._overlay.view_state != "overlay":
            return self._store.get_fact(object_id)
        for fact in self._overlay.fact_upserts:
            if fact.object_id == object_id:
                return fact
        if object_id in self._overlay.fact_tombstones:
            return None
        fact = self._store.get_fact(object_id)
        if fact is not None and self._is_fact_hidden(fact):
            return None
        return fact

    def search(self, query: str, limit: int = 20) -> List[FactRecord]:
        if self._overlay is None or self._overlay.view_state != "overlay":
            return self._store.search(query, limit)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise StorageError("invalid_limit", "limit must be >= 1")
        if not isinstance(query, str):
            raise StorageError("invalid_query", "query must be a string")
        upsert_ids = {fact.object_id for fact in self._overlay.fact_upserts}
        base_limit = max(limit + len(upsert_ids) + len(self._overlay.fact_tombstones) + len(self._overlay.source_tombstones) * 8, limit)
        base_limit = min(max(base_limit, 32), 10_000)
        base_results: List[FactRecord] = []
        for _attempt in range(5):
            candidates = self._store.search(query, base_limit)
            base_results = [
                fact
                for fact in candidates
                if fact.object_id not in upsert_ids and not self._is_fact_hidden(fact)
            ]
            if len(base_results) + len(self._overlay.fact_upserts) >= limit or len(candidates) < base_limit:
                break
            next_limit = min(base_limit * 2, 10_000)
            if next_limit == base_limit:
                break
            base_limit = next_limit
        return _search_facts([*base_results, *self._overlay.fact_upserts], query, limit)

    def relation_search(self, query: str, limit: int = 20) -> RelationSearchResult:
        if self._overlay is None or self._overlay.view_state != "overlay":
            return self._store.relation_search(query, limit)
        started = _now()
        if not isinstance(query, str):
            error = StorageError("invalid_query", "query must be a string")
            self._store._emit_storage_error(error, "search", started)
            raise error
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            error = StorageError("invalid_limit", "limit must be >= 1")
            self._store._emit_storage_error(error, "search", started)
            raise error
        try:
            result = _relation_search_from_records(
                query,
                list(self._iter_visible_facts()),
                self._visible_relatives(),
                limit,
            )
        except StorageError as exc:
            self._store._emit_storage_error(exc, "search", started)
            raise
        self._store._emit_relation_search_event(result, query, limit, started)
        return result

    def relatives_for_fact(
        self,
        fact_id: str,
        direction: str = "both",
        relation_kind: Optional[str] = None,
        limit: int = 20,
    ) -> List[FactRelative]:
        if self._overlay is None or self._overlay.view_state != "overlay":
            return self._store.relatives_for_fact(fact_id, direction, relation_kind, limit)
        return _filter_relatives(self._get_overlay_relatives(), fact_id, direction, relation_kind, limit)

    def count_relatives_for_fact(
        self,
        fact_id: str,
        direction: str = "both",
        relation_kind: Optional[str] = None,
    ) -> int:
        if self._overlay is None or self._overlay.view_state != "overlay":
            return self._store.count_relatives_for_fact(fact_id, direction, relation_kind)
        _validate_relative_query(fact_id, direction, relation_kind)
        return sum(
            1
            for relative in self._get_overlay_relatives()
            if _relative_matches(relative, fact_id, direction, relation_kind)
        )

    def stats(self) -> "StorageStats":
        stats = self._store.stats()
        if self._overlay is None or self._overlay.view_state != "overlay":
            return stats
        facts = list(self._iter_visible_facts())
        relatives = [
            relative
            for relative in self._store.iter_relatives()
            if not self._is_relative_hidden(relative)
            and not any(self._is_fact_id_hidden(endpoint) for endpoint in (relative.from_fact_id, relative.to_fact_id))
        ]
        relatives.extend(self._overlay.relative_upserts)
        return replace(
            stats,
            total_facts=len(facts),
            total_relatives=len(relatives),
            fact_kinds=dict(sorted(Counter(_fact_kind(fact) for fact in facts).items())),
            relation_kinds=dict(sorted(Counter(relative.relation_kind for relative in relatives).items())),
            conditional_relative_count=sum(1 for relative in relatives if relative.condition is not None),
        )

    def _is_fact_hidden(self, fact: FactRecord) -> bool:
        if self._overlay is None:
            return False
        return fact.object_id in self._overlay.fact_tombstones or _fact_source_id(fact) in self._overlay.source_tombstones

    def _is_fact_id_hidden(self, object_id: str) -> bool:
        if self._overlay is None:
            return False
        if object_id in self._overlay.fact_tombstones:
            return True
        fact = self._store.get_fact(object_id)
        return fact is not None and _fact_source_id(fact) in self._overlay.source_tombstones

    def _is_relative_hidden(self, relative: FactRelative) -> bool:
        if self._overlay is None:
            return False
        return (
            relative.relative_id in self._overlay.relative_tombstones
            or _relative_source_id(relative) in self._overlay.source_tombstones
        )

    def _visible_relatives(self) -> List[FactRelative]:
        if self._overlay is None or self._overlay.view_state != "overlay":
            return list(self._store.iter_relatives())
        return self._get_overlay_relatives()

    def _get_overlay_relatives(self) -> List[FactRelative]:
        cached = self._overlay_relatives_cache
        if cached is not None:
            return cached
        visible_facts = {fact.object_id for fact in self._iter_visible_facts()}
        relatives = [
            relative
            for relative in self._store.iter_relatives()
            if not self._is_relative_hidden(relative)
            and relative.from_fact_id in visible_facts
            and relative.to_fact_id in visible_facts
        ]
        upsert_ids = {relative.relative_id for relative in self._overlay.relative_upserts}
        relatives = [relative for relative in relatives if relative.relative_id not in upsert_ids]
        relatives.extend(self._overlay.relative_upserts)
        self._overlay_relatives_cache = relatives
        return relatives

    def _iter_visible_facts(self) -> Iterator[FactRecord]:
        if self._overlay is None:
            yield from self._store.iter_facts()
            return
        upsert_ids = {fact.object_id for fact in self._overlay.fact_upserts}
        for fact in self._store.iter_facts():
            if fact.object_id not in upsert_ids and not self._is_fact_hidden(fact):
                yield fact
        yield from self._overlay.fact_upserts


@dataclass(frozen=True)
class StorageManifest:
    schema_version: int
    snapshot_id: str
    snapshot_format: str
    compression: str
    reused: bool
    created_at: str
    fact_count: int
    relative_count: int
    source_count: int
    facts_sha256: str
    relatives_sha256: str
    source_inventory_sha256: str
    bytes_on_disk: int
    uncompressed_bytes: int
    compressed_data_bytes: int
    compression_ratio: float
    storage_overhead_ratio: float
    file_bytes: Dict[str, Dict[str, JSONValue]]
    read_index: Dict[str, JSONValue]
    stats: Dict[str, JSONValue]
    log_write_failures: int
    latest_log_error_code: Optional[str]

    @classmethod
    def from_json(cls, row: Dict[str, Any]) -> "StorageManifest":
        if not isinstance(row, dict):
            raise StorageError("snapshot_corrupt", "manifest must be a JSON object")
        if row.get("schema_version") != SCHEMA_VERSION:
            raise StorageError("unsupported_schema_version", "unsupported manifest schema version")
        if row.get("snapshot_format") != SNAPSHOT_FORMAT or row.get("compression") != SNAPSHOT_COMPRESSION:
            raise StorageError("unsupported_schema_version", "unsupported snapshot format")
        required = (
            "snapshot_id",
            "snapshot_format",
            "compression",
            "reused",
            "created_at",
            "fact_count",
            "relative_count",
            "source_count",
            "facts_sha256",
            "relatives_sha256",
            "source_inventory_sha256",
            "bytes_on_disk",
            "uncompressed_bytes",
            "compressed_data_bytes",
            "compression_ratio",
            "storage_overhead_ratio",
            "file_bytes",
            "read_index",
            "stats",
            "log_write_failures",
            "latest_log_error_code",
        )
        for field_name in required:
            if field_name not in row:
                raise StorageError("snapshot_corrupt", f"manifest missing {field_name}")
        return cls(schema_version=row["schema_version"], **{key: row[key] for key in required})

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "snapshot_format": self.snapshot_format,
            "compression": self.compression,
            "reused": self.reused,
            "created_at": self.created_at,
            "fact_count": self.fact_count,
            "relative_count": self.relative_count,
            "source_count": self.source_count,
            "facts_sha256": self.facts_sha256,
            "relatives_sha256": self.relatives_sha256,
            "source_inventory_sha256": self.source_inventory_sha256,
            "bytes_on_disk": self.bytes_on_disk,
            "uncompressed_bytes": self.uncompressed_bytes,
            "compressed_data_bytes": self.compressed_data_bytes,
            "compression_ratio": self.compression_ratio,
            "storage_overhead_ratio": self.storage_overhead_ratio,
            "file_bytes": self.file_bytes,
            "read_index": self.read_index,
            "stats": self.stats,
            "log_write_failures": self.log_write_failures,
            "latest_log_error_code": self.latest_log_error_code,
        }


@dataclass(frozen=True)
class StorageStats:
    total_facts: int
    total_relatives: int
    total_sources: int
    fact_kinds: Dict[str, int]
    relation_kinds: Dict[str, int]
    conditional_relative_count: int
    orphan_relative_count: int
    profiles: Dict[str, int]
    source_files: Dict[str, int]
    with_caller_count: int
    with_callee_count: int
    snapshot_count: int
    bytes_on_disk: int
    bytes_on_disk_total: int
    uncompressed_bytes: int
    compressed_data_bytes: int
    compression_ratio: float
    storage_overhead_ratio: float
    file_bytes: Dict[str, Dict[str, JSONValue]]
    read_index_state: str
    read_index_bytes: int
    read_index_schema_version: Optional[int]
    read_index_codec: Optional[str]
    snapshot_format: Optional[str]
    compression: Optional[str]
    snapshot_id: Optional[str]
    last_updated: Optional[str]
    log_write_failures: int
    latest_log_error_code: Optional[str]
    lock_state: str

    @classmethod
    def from_json(cls, row: Dict[str, Any]) -> "StorageStats":
        return cls(
            total_facts=row.get("total_facts", 0),
            total_relatives=row.get("total_relatives", 0),
            total_sources=row.get("total_sources", 0),
            fact_kinds=dict(row.get("fact_kinds", {})),
            relation_kinds=dict(row.get("relation_kinds", {})),
            conditional_relative_count=row.get("conditional_relative_count", 0),
            orphan_relative_count=row.get("orphan_relative_count", 0),
            profiles=dict(row.get("profiles", {})),
            source_files=dict(row.get("source_files", {})),
            with_caller_count=row.get("with_caller_count", 0),
            with_callee_count=row.get("with_callee_count", 0),
            snapshot_count=row.get("snapshot_count", 0),
            bytes_on_disk=row.get("bytes_on_disk", 0),
            bytes_on_disk_total=row.get("bytes_on_disk_total", 0),
            uncompressed_bytes=row.get("uncompressed_bytes", 0),
            compressed_data_bytes=row.get("compressed_data_bytes", 0),
            compression_ratio=row.get("compression_ratio", 1.0),
            storage_overhead_ratio=row.get("storage_overhead_ratio", 1.0),
            file_bytes=dict(row.get("file_bytes", {})),
            read_index_state=row.get("read_index_state", "missing"),
            read_index_bytes=row.get("read_index_bytes", 0),
            read_index_schema_version=row.get("read_index_schema_version"),
            read_index_codec=row.get("read_index_codec"),
            snapshot_format=row.get("snapshot_format"),
            compression=row.get("compression"),
            snapshot_id=row.get("snapshot_id"),
            last_updated=row.get("last_updated"),
            log_write_failures=row.get("log_write_failures", 0),
            latest_log_error_code=row.get("latest_log_error_code"),
            lock_state=row.get("lock_state", "free"),
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "total_facts": self.total_facts,
            "total_relatives": self.total_relatives,
            "total_sources": self.total_sources,
            "fact_kinds": self.fact_kinds,
            "relation_kinds": self.relation_kinds,
            "conditional_relative_count": self.conditional_relative_count,
            "orphan_relative_count": self.orphan_relative_count,
            "profiles": self.profiles,
            "source_files": self.source_files,
            "with_caller_count": self.with_caller_count,
            "with_callee_count": self.with_callee_count,
            "snapshot_count": self.snapshot_count,
            "bytes_on_disk": self.bytes_on_disk,
            "bytes_on_disk_total": self.bytes_on_disk_total,
            "uncompressed_bytes": self.uncompressed_bytes,
            "compressed_data_bytes": self.compressed_data_bytes,
            "compression_ratio": self.compression_ratio,
            "storage_overhead_ratio": self.storage_overhead_ratio,
            "file_bytes": self.file_bytes,
            "read_index_state": self.read_index_state,
            "read_index_bytes": self.read_index_bytes,
            "read_index_schema_version": self.read_index_schema_version,
            "read_index_codec": self.read_index_codec,
            "snapshot_format": self.snapshot_format,
            "compression": self.compression,
            "snapshot_id": self.snapshot_id,
            "last_updated": self.last_updated,
            "log_write_failures": self.log_write_failures,
            "latest_log_error_code": self.latest_log_error_code,
            "lock_state": self.lock_state,
        }

__all__ = [name for name in globals() if not name.startswith("__")]

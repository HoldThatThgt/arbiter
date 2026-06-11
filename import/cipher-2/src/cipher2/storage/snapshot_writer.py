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
from .views import *
from .search import *
from .serialization import *
from .read_index import *
from .utils import *

def _build_read_index_file(self, snapshot_dir: Path, snapshot_id: str, prepared: Dict[str, Any]) -> Dict[str, JSONValue]:
    read_index_path = snapshot_dir / READ_INDEX_FILE
    if read_index_path.exists():
        read_index_path.unlink()
    connection = sqlite3.connect(str(read_index_path))
    try:
        connection.execute("PRAGMA page_size=1024")
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID")
        connection.execute(
            """
            CREATE TABLE facts (
                object_id TEXT PRIMARY KEY,
                object_name TEXT NOT NULL,
                object_description TEXT NOT NULL,
                object_source TEXT NOT NULL,
                object_profile TEXT NOT NULL,
                object_caller TEXT,
                object_callee TEXT,
                payload_json TEXT NOT NULL,
                fact_kind_rank INTEGER NOT NULL,
                object_name_cf TEXT,
                object_description_cf TEXT,
                object_caller_cf TEXT,
                object_callee_cf TEXT,
                object_source_cf TEXT
            ) WITHOUT ROWID
            """
        )
        connection.execute(
            """
            CREATE TABLE fact_keys (
                fact_k INTEGER PRIMARY KEY,
                object_id TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )
        connection.execute("CREATE UNIQUE INDEX fact_keys_object_id_idx ON fact_keys(object_id)")
        connection.execute(
            """
            CREATE TABLE relative_ids (
                relative_k INTEGER PRIMARY KEY,
                relative_id TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )
        connection.execute(
            """
            CREATE TABLE relatives (
                relative_k INTEGER PRIMARY KEY,
                from_k INTEGER NOT NULL,
                to_k INTEGER NOT NULL,
                relation_kind_code INTEGER NOT NULL,
                confidence REAL NOT NULL,
                object_profile TEXT,
                evidence_source TEXT,
                condition_json TEXT,
                payload_json TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )
        connection.execute("CREATE INDEX relatives_from_idx ON relatives(from_k, relation_kind_code)")
        connection.execute("CREATE INDEX relatives_to_idx ON relatives(to_k, relation_kind_code)")

        fact_count = 0
        fact_batch: List[Tuple[Any, ...]] = []
        fact_key_batch: List[Tuple[int, str]] = []
        previous_fact_id: Optional[str] = None
        write_fact_keys = prepared["relative_count"] > 0
        facts_path = snapshot_dir / SNAPSHOT_DATA_FILES["facts"]
        for _line_number, _raw_line, raw_text in _iter_gzip_raw_lines(
            facts_path,
            missing_message="facts.jsonl.gz is missing",
        ):
            if not raw_text:
                continue
            fact = _validated_fact_from_raw_text(raw_text)
            previous_fact_id = _require_next_sorted_unique_id(
                previous_fact_id,
                fact.object_id,
                duplicate_code="duplicate_object_id",
                unsorted_code="unsorted_object_id",
            )
            fact_count += 1
            fact_batch.append(_fact_index_tuple(fact))
            if write_fact_keys:
                fact_key_batch.append((fact_count, fact.object_id))
            if len(fact_batch) >= 1000:
                _insert_persistent_fact_index_batch(connection, fact_batch)
                fact_batch.clear()
                if fact_key_batch:
                    _insert_persistent_fact_key_batch(connection, fact_key_batch)
                    fact_key_batch.clear()
        if fact_batch:
            _insert_persistent_fact_index_batch(connection, fact_batch)
        if fact_key_batch:
            _insert_persistent_fact_key_batch(connection, fact_key_batch)
        if fact_count != prepared["fact_count"]:
            raise StorageError("snapshot_corrupt", "facts.jsonl.gz row count differs from manifest", path=facts_path)

        relative_count = 0
        previous_relative_id: Optional[str] = None
        relative_batch: List[Tuple[int, FactRelative]] = []
        relatives_path = snapshot_dir / SNAPSHOT_DATA_FILES["relatives"]
        for _line_number, _raw_line, raw_text in _iter_gzip_raw_lines(
            relatives_path,
            missing_message="relatives.jsonl.gz is missing",
        ):
            if not raw_text:
                continue
            relative = _validated_relative_from_raw_text(raw_text)
            previous_relative_id = _require_next_sorted_unique_id(
                previous_relative_id,
                relative.relative_id,
                duplicate_code="duplicate_relative_id",
                unsorted_code="unsorted_relative_id",
            )
            relative_count += 1
            relative_batch.append((relative_count, relative))
            if len(relative_batch) >= 1000:
                _insert_persistent_relative_index_batch(connection, relative_batch)
                relative_batch.clear()
        if relative_batch:
            _insert_persistent_relative_index_batch(connection, relative_batch)
        if relative_count != prepared["relative_count"]:
            raise StorageError("snapshot_corrupt", "relatives.jsonl.gz row count differs from manifest", path=relatives_path)

        metadata = {
            "snapshot_id": snapshot_id,
            "schema_version": str(READ_INDEX_SCHEMA_VERSION),
            "facts_sha256": prepared["facts_sha256"],
            "relatives_sha256": prepared["relatives_sha256"],
            "source_inventory_sha256": prepared["source_inventory_sha256"],
            "fact_count": str(prepared["fact_count"]),
            "relative_count": str(prepared["relative_count"]),
            "source_count": str(prepared["source_count"]),
            "projection_kind": READ_INDEX_PROJECTION_KIND,
            "payload_codec": READ_INDEX_PAYLOAD_CODEC,
        }
        connection.executemany(
            "INSERT INTO index_metadata(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
        connection.commit()
    except Exception:
        connection.close()
        if read_index_path.exists():
            read_index_path.unlink()
        for sidecar in READ_INDEX_SIDECARS:
            sidecar_path = snapshot_dir / sidecar
            if sidecar_path.exists():
                sidecar_path.unlink()
        raise
    connection.close()
    for sidecar in READ_INDEX_SIDECARS:
        sidecar_path = snapshot_dir / sidecar
        if sidecar_path.exists():
            raise StorageError("snapshot_corrupt", "SQLite read index sidecar file leaked into snapshot", path=sidecar_path)
    return {
        "file_name": READ_INDEX_FILE,
        "index_format": READ_INDEX_FORMAT,
        "schema_version": READ_INDEX_SCHEMA_VERSION,
        "projection_kind": READ_INDEX_PROJECTION_KIND,
        "payload_codec": READ_INDEX_PAYLOAD_CODEC,
        "bytes_on_disk": read_index_path.stat().st_size,
        "fact_count": fact_count,
        "relative_count": relative_count,
    }

def _prepare_snapshot_staging(
    self,
    staging_dir: Path,
    facts: Iterable[FactRecord],
    relatives: Iterable[FactRelative],
    source_inventory: Iterable[SourceInventoryEntry],
) -> Dict[str, Any]:
    db_path = staging_dir / "facts.sqlite"
    facts_path = staging_dir / SNAPSHOT_DATA_FILES["facts"]
    relatives_path = staging_dir / SNAPSHOT_DATA_FILES["relatives"]
    source_inventory_path = staging_dir / SNAPSHOT_DATA_FILES["source_inventory"]
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("CREATE TABLE facts (object_id TEXT PRIMARY KEY, line TEXT NOT NULL)")
        connection.execute(
            """
            CREATE TABLE relatives (
                relative_id TEXT PRIMARY KEY,
                from_fact_id TEXT NOT NULL,
                to_fact_id TEXT NOT NULL,
                line TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE TABLE source_inventory (source_id TEXT PRIMARY KEY, line TEXT NOT NULL)")
        fact_count = 0
        fact_kinds: Counter[str] = Counter()
        profiles: Counter[str] = Counter()
        source_files: Counter[str] = Counter()
        with_caller_count = 0
        with_callee_count = 0
        for fact in facts:
            if not isinstance(fact, FactRecord):
                raise StorageError("invalid_fact", "replace_snapshot expects FactRecord items")
            line = StoredFactLine.from_fact(fact)
            line_text = _canonical_json(line.to_json()) + "\n"
            try:
                connection.execute("INSERT INTO facts(object_id, line) VALUES (?, ?)", (line.object_id, line_text))
            except sqlite3.IntegrityError as exc:
                raise StorageError("duplicate_object_id", "duplicate object_id") from exc
            fact_count += 1
            fact_kinds[line.fact_kind] += 1
            profiles[fact.object_profile] += 1
            source_files[_source_bucket(fact.object_source)] += 1
            if fact.object_caller:
                with_caller_count += 1
            if fact.object_callee:
                with_callee_count += 1
            if fact_count % SNAPSHOT_STAGING_COMMIT_INTERVAL == 0:
                connection.commit()

        relative_count = 0
        relation_kinds: Counter[str] = Counter()
        conditional_relative_count = 0
        for relative in relatives:
            if not isinstance(relative, FactRelative):
                raise StorageError("invalid_relative", "replace_snapshot expects FactRelative items")
            line = StoredRelativeLine.from_relative(relative)
            line_text = _canonical_json(line.to_json()) + "\n"
            try:
                connection.execute(
                    "INSERT INTO relatives(relative_id, from_fact_id, to_fact_id, line) VALUES (?, ?, ?, ?)",
                    (line.relative_id, line.from_fact_id, line.to_fact_id, line_text),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError("duplicate_relative_id", "duplicate relative_id") from exc
            relative_count += 1
            relation_kinds[line.relation_kind] += 1
            if line.condition is not None:
                conditional_relative_count += 1
            if relative_count % SNAPSHOT_STAGING_COMMIT_INTERVAL == 0:
                connection.commit()
        missing_endpoint = connection.execute(
            """
            SELECT 1
            FROM relatives AS r
            LEFT JOIN facts AS from_fact ON from_fact.object_id = r.from_fact_id
            LEFT JOIN facts AS to_fact ON to_fact.object_id = r.to_fact_id
            WHERE from_fact.object_id IS NULL OR to_fact.object_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if missing_endpoint is not None:
            raise StorageError("relative_endpoint_missing", "relative endpoint is missing")
        source_count = 0
        for entry in source_inventory:
            if not isinstance(entry, SourceInventoryEntry):
                raise StorageError("invalid_source_inventory", "replace_snapshot expects SourceInventoryEntry items")
            line = StoredSourceInventoryLine.from_entry(entry)
            line_text = _canonical_json(line.to_json()) + "\n"
            try:
                connection.execute("INSERT INTO source_inventory(source_id, line) VALUES (?, ?)", (line.source_id, line_text))
            except sqlite3.IntegrityError as exc:
                raise StorageError("duplicate_source_id", "duplicate source_id") from exc
            source_count += 1
            if source_count % SNAPSHOT_STAGING_COMMIT_INTERVAL == 0:
                connection.commit()
        connection.commit()
        facts_sha256, facts_size, facts_compressed_size = _write_gzip_jsonl(
            facts_path,
            (line_text for (line_text,) in connection.execute("SELECT line FROM facts ORDER BY object_id")),
        )
        relatives_sha256, relatives_size, relatives_compressed_size = _write_gzip_jsonl(
            relatives_path,
            (line_text for (line_text,) in connection.execute("SELECT line FROM relatives ORDER BY relative_id")),
        )
        source_inventory_sha256, source_inventory_size, source_inventory_compressed_size = _write_gzip_jsonl(
            source_inventory_path,
            (line_text for (line_text,) in connection.execute("SELECT line FROM source_inventory ORDER BY source_id")),
        )
    finally:
        connection.close()
        if db_path.exists():
            db_path.unlink()
    return {
        "facts_sha256": facts_sha256,
        "relatives_sha256": relatives_sha256,
        "source_inventory_sha256": source_inventory_sha256,
        "facts_size": facts_size,
        "relatives_size": relatives_size,
        "source_inventory_size": source_inventory_size,
        "uncompressed_bytes": facts_size + relatives_size + source_inventory_size,
        "compressed_data_bytes": facts_compressed_size + relatives_compressed_size + source_inventory_compressed_size,
        "file_bytes": {
            "facts": _file_metrics(SNAPSHOT_DATA_FILES["facts"], facts_size, facts_compressed_size),
            "relatives": _file_metrics(SNAPSHOT_DATA_FILES["relatives"], relatives_size, relatives_compressed_size),
            "source_inventory": _file_metrics(
                SNAPSHOT_DATA_FILES["source_inventory"],
                source_inventory_size,
                source_inventory_compressed_size,
            ),
        },
        "fact_count": fact_count,
        "relative_count": relative_count,
        "source_count": source_count,
        "fact_kinds": fact_kinds,
        "relation_kinds": relation_kinds,
        "conditional_relative_count": conditional_relative_count,
        "profiles": profiles,
        "source_files": source_files,
        "with_caller_count": with_caller_count,
        "with_callee_count": with_callee_count,
    }

def _prepare_sorted_unique_snapshot_staging(
    self,
    staging_dir: Path,
    facts: Iterable[FactRecord],
    relatives: Iterable[FactRelative],
    source_inventory: Iterable[SourceInventoryEntry],
) -> Dict[str, Any]:
    db_path = staging_dir / "sorted-unique-ids.sqlite"
    facts_path = staging_dir / SNAPSHOT_DATA_FILES["facts"]
    relatives_path = staging_dir / SNAPSHOT_DATA_FILES["relatives"]
    source_inventory_path = staging_dir / SNAPSHOT_DATA_FILES["source_inventory"]
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("CREATE TABLE fact_ids (object_id TEXT PRIMARY KEY) WITHOUT ROWID")
        fact_count = 0
        fact_kinds: Counter[str] = Counter()
        profiles: Counter[str] = Counter()
        source_files: Counter[str] = Counter()
        with_caller_count = 0
        with_callee_count = 0

        def fact_lines() -> Iterator[str]:
            nonlocal fact_count, with_caller_count, with_callee_count
            previous_id: Optional[str] = None
            for fact in facts:
                if not isinstance(fact, FactRecord):
                    raise StorageError("invalid_fact", "replace_snapshot expects FactRecord items")
                line = StoredFactLine.from_fact(fact)
                previous_id = _require_next_sorted_unique_id(
                    previous_id,
                    line.object_id,
                    duplicate_code="duplicate_object_id",
                    unsorted_code="unsorted_object_id",
                )
                try:
                    connection.execute("INSERT INTO fact_ids(object_id) VALUES (?)", (line.object_id,))
                except sqlite3.IntegrityError as exc:
                    raise StorageError("duplicate_object_id", "duplicate object_id") from exc
                fact_count += 1
                fact_kinds[line.fact_kind] += 1
                profiles[fact.object_profile] += 1
                source_files[_source_bucket(fact.object_source)] += 1
                if fact.object_caller:
                    with_caller_count += 1
                if fact.object_callee:
                    with_callee_count += 1
                if fact_count % SNAPSHOT_STAGING_COMMIT_INTERVAL == 0:
                    connection.commit()
                yield _canonical_json(line.to_json()) + "\n"

        facts_sha256, facts_size, facts_compressed_size = _write_gzip_jsonl(facts_path, fact_lines())
        connection.commit()

        relative_count = 0
        relation_kinds: Counter[str] = Counter()
        conditional_relative_count = 0

        def relative_lines() -> Iterator[str]:
            nonlocal relative_count, conditional_relative_count
            previous_id: Optional[str] = None
            for relative in relatives:
                if not isinstance(relative, FactRelative):
                    raise StorageError("invalid_relative", "replace_snapshot expects FactRelative items")
                line = StoredRelativeLine.from_relative(relative)
                previous_id = _require_next_sorted_unique_id(
                    previous_id,
                    line.relative_id,
                    duplicate_code="duplicate_relative_id",
                    unsorted_code="unsorted_relative_id",
                )
                from_exists = connection.execute(
                    "SELECT 1 FROM fact_ids WHERE object_id = ?",
                    (line.from_fact_id,),
                ).fetchone()
                to_exists = connection.execute(
                    "SELECT 1 FROM fact_ids WHERE object_id = ?",
                    (line.to_fact_id,),
                ).fetchone()
                if from_exists is None or to_exists is None:
                    raise StorageError("relative_endpoint_missing", "relative endpoint is missing")
                relative_count += 1
                relation_kinds[line.relation_kind] += 1
                if line.condition is not None:
                    conditional_relative_count += 1
                yield _canonical_json(line.to_json()) + "\n"

        relatives_sha256, relatives_size, relatives_compressed_size = _write_gzip_jsonl(
            relatives_path,
            relative_lines(),
        )

        source_count = 0

        def source_inventory_lines() -> Iterator[str]:
            nonlocal source_count
            previous_id: Optional[str] = None
            for entry in source_inventory:
                if not isinstance(entry, SourceInventoryEntry):
                    raise StorageError("invalid_source_inventory", "replace_snapshot expects SourceInventoryEntry items")
                line = StoredSourceInventoryLine.from_entry(entry)
                previous_id = _require_next_sorted_unique_id(
                    previous_id,
                    line.source_id,
                    duplicate_code="duplicate_source_id",
                    unsorted_code="unsorted_source_id",
                )
                source_count += 1
                yield _canonical_json(line.to_json()) + "\n"

        source_inventory_sha256, source_inventory_size, source_inventory_compressed_size = _write_gzip_jsonl(
            source_inventory_path,
            source_inventory_lines(),
        )
    finally:
        connection.close()
        if db_path.exists():
            db_path.unlink()
    return {
        "facts_sha256": facts_sha256,
        "relatives_sha256": relatives_sha256,
        "source_inventory_sha256": source_inventory_sha256,
        "facts_size": facts_size,
        "relatives_size": relatives_size,
        "source_inventory_size": source_inventory_size,
        "uncompressed_bytes": facts_size + relatives_size + source_inventory_size,
        "compressed_data_bytes": facts_compressed_size + relatives_compressed_size + source_inventory_compressed_size,
        "file_bytes": {
            "facts": _file_metrics(SNAPSHOT_DATA_FILES["facts"], facts_size, facts_compressed_size),
            "relatives": _file_metrics(SNAPSHOT_DATA_FILES["relatives"], relatives_size, relatives_compressed_size),
            "source_inventory": _file_metrics(
                SNAPSHOT_DATA_FILES["source_inventory"],
                source_inventory_size,
                source_inventory_compressed_size,
            ),
        },
        "fact_count": fact_count,
        "relative_count": relative_count,
        "source_count": source_count,
        "fact_kinds": fact_kinds,
        "relation_kinds": relation_kinds,
        "conditional_relative_count": conditional_relative_count,
        "profiles": profiles,
        "source_files": source_files,
        "with_caller_count": with_caller_count,
        "with_callee_count": with_callee_count,
    }

def _prepare_preencoded_sorted_unique_snapshot_staging(
    self,
    staging_dir: Path,
    facts: Iterable[EncodedFactLine],
    relatives: Iterable[EncodedRelativeLine],
    source_inventory: Iterable[SourceInventoryEntry],
) -> Dict[str, Any]:
    db_path = staging_dir / "preencoded-sorted-unique-ids.sqlite"
    facts_path = staging_dir / SNAPSHOT_DATA_FILES["facts"]
    relatives_path = staging_dir / SNAPSHOT_DATA_FILES["relatives"]
    source_inventory_path = staging_dir / SNAPSHOT_DATA_FILES["source_inventory"]
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("CREATE TABLE fact_ids (object_id TEXT PRIMARY KEY) WITHOUT ROWID")
        fact_count = 0
        fact_kinds: Counter[str] = Counter()
        profiles: Counter[str] = Counter()
        source_files: Counter[str] = Counter()
        with_caller_count = 0
        with_callee_count = 0

        def fact_lines() -> Iterator[str]:
            nonlocal fact_count, with_caller_count, with_callee_count
            previous_id: Optional[str] = None
            for fact in facts:
                if not isinstance(fact, EncodedFactLine):
                    raise StorageError("invalid_fact", "preencoded snapshot expects EncodedFactLine items")
                previous_id = _require_next_sorted_unique_id(
                    previous_id,
                    fact.object_id,
                    duplicate_code="duplicate_object_id",
                    unsorted_code="unsorted_object_id",
                )
                try:
                    connection.execute("INSERT INTO fact_ids(object_id) VALUES (?)", (fact.object_id,))
                except sqlite3.IntegrityError as exc:
                    raise StorageError("duplicate_object_id", "duplicate object_id") from exc
                fact_count += 1
                fact_kinds[fact.fact_kind] += 1
                profiles[fact.object_profile] += 1
                source_files[_source_bucket(fact.object_source)] += 1
                if fact.object_caller:
                    with_caller_count += 1
                if fact.object_callee:
                    with_callee_count += 1
                if fact_count % SNAPSHOT_STAGING_COMMIT_INTERVAL == 0:
                    connection.commit()
                yield _ensure_line_ending(fact.read_line_text())

        facts_sha256, facts_size, facts_compressed_size = _write_gzip_jsonl(facts_path, fact_lines())
        connection.commit()

        relative_count = 0
        relation_kinds: Counter[str] = Counter()
        conditional_relative_count = 0

        def relative_lines() -> Iterator[str]:
            nonlocal relative_count, conditional_relative_count
            previous_id: Optional[str] = None
            for relative in relatives:
                if not isinstance(relative, EncodedRelativeLine):
                    raise StorageError("invalid_relative", "preencoded snapshot expects EncodedRelativeLine items")
                previous_id = _require_next_sorted_unique_id(
                    previous_id,
                    relative.relative_id,
                    duplicate_code="duplicate_relative_id",
                    unsorted_code="unsorted_relative_id",
                )
                from_exists = connection.execute(
                    "SELECT 1 FROM fact_ids WHERE object_id = ?",
                    (relative.from_fact_id,),
                ).fetchone()
                to_exists = connection.execute(
                    "SELECT 1 FROM fact_ids WHERE object_id = ?",
                    (relative.to_fact_id,),
                ).fetchone()
                if from_exists is None or to_exists is None:
                    raise StorageError("relative_endpoint_missing", "relative endpoint is missing")
                relative_count += 1
                relation_kinds[relative.relation_kind] += 1
                if relative.condition is not None:
                    conditional_relative_count += 1
                yield _ensure_line_ending(relative.read_line_text())

        relatives_sha256, relatives_size, relatives_compressed_size = _write_gzip_jsonl(
            relatives_path,
            relative_lines(),
        )

        source_count = 0

        def source_inventory_lines() -> Iterator[str]:
            nonlocal source_count
            previous_id: Optional[str] = None
            for entry in source_inventory:
                if not isinstance(entry, SourceInventoryEntry):
                    raise StorageError("invalid_source_inventory", "replace_snapshot expects SourceInventoryEntry items")
                line = StoredSourceInventoryLine.from_entry(entry)
                previous_id = _require_next_sorted_unique_id(
                    previous_id,
                    line.source_id,
                    duplicate_code="duplicate_source_id",
                    unsorted_code="unsorted_source_id",
                )
                source_count += 1
                yield _canonical_json(line.to_json()) + "\n"

        source_inventory_sha256, source_inventory_size, source_inventory_compressed_size = _write_gzip_jsonl(
            source_inventory_path,
            source_inventory_lines(),
        )
    finally:
        connection.close()
        if db_path.exists():
            db_path.unlink()
    return {
        "facts_sha256": facts_sha256,
        "relatives_sha256": relatives_sha256,
        "source_inventory_sha256": source_inventory_sha256,
        "facts_size": facts_size,
        "relatives_size": relatives_size,
        "source_inventory_size": source_inventory_size,
        "uncompressed_bytes": facts_size + relatives_size + source_inventory_size,
        "compressed_data_bytes": facts_compressed_size + relatives_compressed_size + source_inventory_compressed_size,
        "file_bytes": {
            "facts": _file_metrics(SNAPSHOT_DATA_FILES["facts"], facts_size, facts_compressed_size),
            "relatives": _file_metrics(SNAPSHOT_DATA_FILES["relatives"], relatives_size, relatives_compressed_size),
            "source_inventory": _file_metrics(
                SNAPSHOT_DATA_FILES["source_inventory"],
                source_inventory_size,
                source_inventory_compressed_size,
            ),
        },
        "fact_count": fact_count,
        "relative_count": relative_count,
        "source_count": source_count,
        "fact_kinds": fact_kinds,
        "relation_kinds": relation_kinds,
        "conditional_relative_count": conditional_relative_count,
        "profiles": profiles,
        "source_files": source_files,
        "with_caller_count": with_caller_count,
        "with_callee_count": with_callee_count,
    }

def _build_stats(
    self,
    *,
    fact_count: int,
    relative_count: int,
    source_count: int,
    fact_kinds: Counter[str],
    relation_kinds: Counter[str],
    conditional_relative_count: int,
    orphan_relative_count: int,
    profiles: Counter[str],
    source_files: Counter[str],
    with_caller_count: int,
    with_callee_count: int,
    snapshot_id: str,
    created_at: str,
    log_write_failures: int,
    latest_log_error_code: Optional[str],
    bytes_on_disk: int,
    bytes_on_disk_total: int,
    uncompressed_bytes: int,
    compressed_data_bytes: int,
    file_bytes: Dict[str, Dict[str, JSONValue]],
    read_index: Dict[str, JSONValue],
    extra_snapshot_id: Optional[str],
) -> StorageStats:
    snapshot_count = self._snapshot_count(extra_snapshot_id=extra_snapshot_id)
    return StorageStats(
        total_facts=fact_count,
        total_relatives=relative_count,
        total_sources=source_count,
        fact_kinds=dict(sorted(fact_kinds.items())),
        relation_kinds=dict(sorted(relation_kinds.items())),
        conditional_relative_count=conditional_relative_count,
        orphan_relative_count=orphan_relative_count,
        profiles=dict(sorted(profiles.items())),
        source_files=dict(sorted(source_files.items())),
        with_caller_count=with_caller_count,
        with_callee_count=with_callee_count,
        snapshot_count=snapshot_count,
        bytes_on_disk=bytes_on_disk,
        bytes_on_disk_total=bytes_on_disk_total,
        uncompressed_bytes=uncompressed_bytes,
        compressed_data_bytes=compressed_data_bytes,
        compression_ratio=_compression_ratio(compressed_data_bytes, uncompressed_bytes),
        storage_overhead_ratio=_compression_ratio(bytes_on_disk, uncompressed_bytes),
        file_bytes=file_bytes,
        read_index_state="ready" if read_index else "missing",
        read_index_bytes=int(read_index.get("bytes_on_disk", 0)) if read_index else 0,
        read_index_schema_version=int(read_index["schema_version"]) if read_index else None,
        read_index_codec=str(read_index["payload_codec"]) if read_index else None,
        snapshot_format=SNAPSHOT_FORMAT,
        compression=SNAPSHOT_COMPRESSION,
        snapshot_id=snapshot_id,
        last_updated=created_at,
        log_write_failures=log_write_failures,
        latest_log_error_code=latest_log_error_code,
        lock_state=self._lock_state(),
    )

def _write_manifest_and_stats(
    self,
    snapshot_dir: Path,
    manifest: StorageManifest,
) -> StorageManifest:
    bytes_on_disk = 0
    stable_manifest = manifest
    data_bytes = manifest.compressed_data_bytes + int(manifest.read_index["bytes_on_disk"])
    for _attempt in range(12):
        stats = StorageStats.from_json(stable_manifest.stats)
        stats = replace(
            stats,
            bytes_on_disk=bytes_on_disk,
            bytes_on_disk_total=self._all_snapshots_bytes(extra_current=bytes_on_disk, extra_snapshot_id=manifest.snapshot_id),
            compression_ratio=_compression_ratio(manifest.compressed_data_bytes, manifest.uncompressed_bytes),
            storage_overhead_ratio=_compression_ratio(bytes_on_disk, manifest.uncompressed_bytes),
            lock_state=self._lock_state(),
        )
        stable_manifest = replace(
            stable_manifest,
            bytes_on_disk=bytes_on_disk,
            compression_ratio=_compression_ratio(manifest.compressed_data_bytes, manifest.uncompressed_bytes),
            storage_overhead_ratio=_compression_ratio(bytes_on_disk, manifest.uncompressed_bytes),
            stats=stats.to_json(),
            log_write_failures=stats.log_write_failures,
            latest_log_error_code=stats.latest_log_error_code,
        )
        manifest_text = _canonical_metadata_json(stable_manifest.to_json()) + "\n"
        stats_text = _canonical_metadata_json(stats.to_json()) + "\n"
        next_bytes = (
            data_bytes
            + len(manifest_text.encode("utf-8"))
            + len(stats_text.encode("utf-8"))
        )
        if next_bytes == bytes_on_disk:
            (snapshot_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")
            (snapshot_dir / "stats.json").write_text(stats_text, encoding="utf-8")
            return stable_manifest
        bytes_on_disk = next_bytes
    raise StorageError("snapshot_corrupt", "snapshot metadata size did not converge", path=snapshot_dir)

def _write_current(self, snapshots_dir: Path, snapshot_id: str) -> None:
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    tmp = snapshots_dir / "current.tmp"
    tmp.write_text(snapshot_id, encoding="utf-8")
    os.replace(tmp, snapshots_dir / CURRENT_POINTER)

__all__ = [name for name in globals() if not name.startswith("__")]

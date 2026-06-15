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

def _stream_current_facts(self) -> Iterator[FactRecord]:
    for line in self._stream_current_fact_lines(validate_payload_sha=True):
        yield line.to_fact()

def _stream_current_relatives(self) -> Iterator[FactRelative]:
    for line in self._stream_current_relative_lines(validate_payload_sha=True):
        yield line.to_relative()

def _stream_current_source_inventory(self) -> Iterator[SourceInventoryEntry]:
    for raw_text in self._stream_current_source_inventory_raw_lines():
        try:
            row = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise StorageError("snapshot_corrupt", "source_inventory.jsonl contains malformed JSON") from exc
        yield StoredSourceInventoryLine.from_json(row).to_entry()

def _stream_current_fact_lines(self, *, validate_payload_sha: bool) -> Iterator[StoredFactLine]:
    for raw_text in self._stream_current_fact_raw_lines():
        try:
            row = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise StorageError("snapshot_corrupt", "facts.jsonl contains malformed JSON") from exc
        if validate_payload_sha:
            yield StoredFactLine.from_json(row)
        else:
            yield _stored_fact_line_without_payload_validation(row)

def _stream_current_fact_raw_lines(self) -> Iterator[str]:
    current = self._current_snapshot_dir()
    if current is None:
        return
    manifest, _stats = self._load_metadata(current, validate_actual_bytes=False)
    facts_path = current / SNAPSHOT_DATA_FILES["facts"]
    digest = hashlib.sha256()
    for _line_number, raw_line, raw_text in _iter_gzip_raw_lines(
        facts_path,
        missing_message="facts.jsonl.gz is missing",
    ):
        digest.update(raw_line)
        if raw_text:
            yield raw_text
    facts_sha256 = digest.hexdigest()
    if facts_sha256 != manifest.facts_sha256:
        raise StorageError("manifest_mismatch", "facts.jsonl.gz hash differs from manifest", path=facts_path)
    self._assert_metadata_consistent(current, manifest)

def _stream_current_relative_lines(self, *, validate_payload_sha: bool) -> Iterator[StoredRelativeLine]:
    for raw_text in self._stream_current_relative_raw_lines():
        try:
            row = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise StorageError("snapshot_corrupt", "relatives.jsonl contains malformed JSON") from exc
        if validate_payload_sha:
            yield StoredRelativeLine.from_json(row)
        else:
            yield _stored_relative_line_without_payload_validation(row)

def _stream_current_relative_raw_lines(self) -> Iterator[str]:
    current = self._current_snapshot_dir()
    if current is None:
        return
    manifest, _stats = self._load_metadata(current, validate_actual_bytes=False)
    relatives_path = current / SNAPSHOT_DATA_FILES["relatives"]
    digest = hashlib.sha256()
    for _line_number, raw_line, raw_text in _iter_gzip_raw_lines(
        relatives_path,
        missing_message="relatives.jsonl.gz is missing",
    ):
        digest.update(raw_line)
        if raw_text:
            yield raw_text
    relatives_sha256 = digest.hexdigest()
    if relatives_sha256 != manifest.relatives_sha256:
        raise StorageError("manifest_mismatch", "relatives.jsonl.gz hash differs from manifest", path=relatives_path)
    self._assert_metadata_consistent(current, manifest)

def _stream_current_source_inventory_raw_lines(self) -> Iterator[str]:
    current = self._current_snapshot_dir()
    if current is None:
        return
    manifest, _stats = self._load_metadata(current, validate_actual_bytes=False)
    source_inventory_path = current / SNAPSHOT_DATA_FILES["source_inventory"]
    digest = hashlib.sha256()
    for _line_number, raw_line, raw_text in _iter_gzip_raw_lines(
        source_inventory_path,
        missing_message="source_inventory.jsonl.gz is missing",
    ):
        digest.update(raw_line)
        if raw_text:
            yield raw_text
    source_inventory_sha256 = digest.hexdigest()
    if source_inventory_sha256 != manifest.source_inventory_sha256:
        raise StorageError("manifest_mismatch", "source_inventory.jsonl.gz hash differs from manifest", path=source_inventory_path)
    self._assert_metadata_consistent(current, manifest)

def _read_index(self) -> Optional[_ReadIndex]:
    current = self._current_snapshot_dir()
    if current is None:
        return None
    started = _now()
    manifest, _stats = self._load_metadata(current, validate_actual_bytes=False)
    self._assert_snapshot_file_sizes(current, manifest)
    read_index_path = current / READ_INDEX_FILE
    try:
        read_index_stat = read_index_path.stat()
    except OSError as exc:
        raise StorageError("manifest_mismatch", "read_index.sqlite is missing", path=read_index_path) from exc
    key = (
        str(current.resolve(strict=False)),
        manifest.facts_sha256,
        manifest.relatives_sha256,
        manifest.source_inventory_sha256,
        manifest.fact_count,
        manifest.relative_count,
        manifest.source_count,
        manifest.read_index["schema_version"],
        read_index_stat.st_size,
        read_index_stat.st_mtime_ns,
    )
    with _READ_INDEX_CACHE_LOCK:
        cached = _READ_INDEX_CACHE.get(key)
        if cached is not None:
            _READ_INDEX_CACHE.move_to_end(key)
            self._emit_event(
                "storage.index_open",
                status="ok",
                operation="index_open",
                outcome="cache_hit",
                started=started,
                index_backend="persistent-sqlite",
                read_index_open_ms=round(_elapsed_ms(started)),
                read_index_bytes=int(manifest.read_index["bytes_on_disk"]),
            )
            return cached
    index = self._open_persistent_read_index(current, manifest)
    with _READ_INDEX_CACHE_LOCK:
        existing = _READ_INDEX_CACHE.get(key)
        if existing is not None:
            index.close()
            _READ_INDEX_CACHE.move_to_end(key)
            self._emit_event(
                "storage.index_open",
                status="ok",
                operation="index_open",
                outcome="cache_hit",
                started=started,
                index_backend="persistent-sqlite",
                read_index_open_ms=round(_elapsed_ms(started)),
                read_index_bytes=int(manifest.read_index["bytes_on_disk"]),
            )
            return existing
        _READ_INDEX_CACHE[key] = index
        while len(_READ_INDEX_CACHE) > READ_INDEX_CACHE_LIMIT:
            _old_key, old_index = _READ_INDEX_CACHE.popitem(last=False)
            old_index.close()
        self._emit_event(
            "storage.index_open",
            status="ok",
            operation="index_open",
            outcome="opened",
            started=started,
            index_backend="persistent-sqlite",
            read_index_open_ms=round(_elapsed_ms(started)),
            read_index_bytes=int(manifest.read_index["bytes_on_disk"]),
        )
        return index

def _open_persistent_read_index(self, snapshot_dir: Path, manifest: StorageManifest) -> _ReadIndex:
    read_index_path = snapshot_dir / READ_INDEX_FILE
    if not read_index_path.exists():
        raise StorageError("manifest_mismatch", "read_index.sqlite is missing; republish the snapshot (re-run the build)", path=read_index_path)
    uri = read_index_path.resolve(strict=False).as_uri() + "?mode=ro&immutable=1"
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        connection.execute("PRAGMA query_only=ON")
        self._validate_read_index_metadata(connection, manifest)
        return _ReadIndex(connection)
    except StorageError:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        raise
    except sqlite3.DatabaseError as exc:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        raise StorageError("snapshot_corrupt", "read_index.sqlite is not a valid SQLite index", path=read_index_path) from exc

def _validate_read_index_metadata(self, connection: sqlite3.Connection, manifest: StorageManifest) -> None:
    try:
        rows = connection.execute("SELECT key, value FROM index_metadata").fetchall()
    except sqlite3.DatabaseError as exc:
        raise StorageError("snapshot_corrupt", "read_index.sqlite metadata is unreadable") from exc
    metadata = {str(key): str(value) for key, value in rows}
    expected = {
        "snapshot_id": manifest.snapshot_id,
        "schema_version": str(READ_INDEX_SCHEMA_VERSION),
        "facts_sha256": manifest.facts_sha256,
        "relatives_sha256": manifest.relatives_sha256,
        "source_inventory_sha256": manifest.source_inventory_sha256,
        "fact_count": str(manifest.fact_count),
        "relative_count": str(manifest.relative_count),
        "source_count": str(manifest.source_count),
        "projection_kind": READ_INDEX_PROJECTION_KIND,
        "payload_codec": READ_INDEX_PAYLOAD_CODEC,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise StorageError("manifest_mismatch", f"read_index.sqlite metadata mismatch for {key}")

def _load_metadata(self, snapshot_dir: Path, *, validate_actual_bytes: bool) -> Tuple[StorageManifest, StorageStats]:
    manifest_path = snapshot_dir / "manifest.json"
    stats_path = snapshot_dir / "stats.json"
    try:
        manifest = StorageManifest.from_json(json.loads(manifest_path.read_text(encoding="utf-8")))
        stats_data = json.loads(stats_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StorageError("manifest_mismatch", "manifest or stats is missing", path=snapshot_dir) from exc
    except json.JSONDecodeError as exc:
        raise StorageError("snapshot_corrupt", "manifest or stats is malformed", path=snapshot_dir) from exc
    for logical_name, file_name in SNAPSHOT_DATA_FILES.items():
        if not (snapshot_dir / file_name).exists():
            raise StorageError("manifest_mismatch", f"{file_name} is missing", path=snapshot_dir / file_name)
        metrics = manifest.file_bytes.get(logical_name)
        if not isinstance(metrics, dict) or metrics.get("file_name") != file_name:
            raise StorageError("manifest_mismatch", f"{logical_name} file metrics are missing", path=snapshot_dir)
    if not isinstance(manifest.read_index, dict) or manifest.read_index.get("file_name") != READ_INDEX_FILE:
        raise StorageError("manifest_mismatch", "read index metrics are missing", path=snapshot_dir)
    if not (snapshot_dir / READ_INDEX_FILE).exists():
        raise StorageError("manifest_mismatch", "read_index.sqlite is missing; republish the snapshot (re-run the build)", path=snapshot_dir / READ_INDEX_FILE)
    stats = StorageStats.from_json(stats_data)
    if manifest.stats != stats_data:
        raise StorageError("stats_mismatch", "manifest stats differs from stats.json", path=stats_path)
    if manifest.bytes_on_disk != stats.bytes_on_disk:
        raise StorageError("stats_mismatch", "manifest bytes differ from stats bytes", path=manifest_path)
    if validate_actual_bytes:
        self._assert_metadata_consistent(snapshot_dir, manifest)
    return manifest, stats

def _assert_metadata_consistent(self, snapshot_dir: Path, manifest: StorageManifest) -> None:
    self._assert_snapshot_file_sizes(snapshot_dir, manifest)
    actual = _snapshot_bytes(snapshot_dir)
    if actual != manifest.bytes_on_disk:
        raise StorageError("stats_mismatch", "actual snapshot bytes differ from manifest", path=snapshot_dir)

def _assert_snapshot_file_sizes(self, snapshot_dir: Path, manifest: StorageManifest) -> None:
    for logical_name, file_name in SNAPSHOT_DATA_FILES.items():
        path = snapshot_dir / file_name
        try:
            actual = path.stat().st_size
        except OSError as exc:
            raise StorageError("manifest_mismatch", f"{file_name} is missing", path=path) from exc
        expected = int(manifest.file_bytes[logical_name]["compressed_bytes"])
        if actual != expected:
            raise StorageError("manifest_mismatch", f"{file_name} size differs from manifest", path=path)
    read_index_path = snapshot_dir / READ_INDEX_FILE
    try:
        actual_index = read_index_path.stat().st_size
    except OSError as exc:
        raise StorageError("manifest_mismatch", "read_index.sqlite is missing; republish the snapshot (re-run the build)", path=read_index_path) from exc
    if actual_index != int(manifest.read_index["bytes_on_disk"]):
        raise StorageError("manifest_mismatch", "read_index.sqlite size differs from manifest", path=read_index_path)

def _read_manifest_if_exists(self, snapshot_dir: Path) -> Optional[StorageManifest]:
    path = snapshot_dir / "manifest.json"
    if not path.exists():
        return None
    return StorageManifest.from_json(json.loads(path.read_text(encoding="utf-8")))

def _current_snapshot_dir(self) -> Optional[Path]:
    current = self._safe_path("snapshots", CURRENT_POINTER)
    if not current.exists():
        return None
    snapshot_id = current.read_text(encoding="utf-8")
    snapshot_dir = self._safe_path("snapshots", snapshot_id)
    if not snapshot_dir.exists():
        raise StorageError("missing_snapshot", "current points to a missing snapshot", path=snapshot_dir)
    return snapshot_dir

__all__ = [name for name in globals() if not name.startswith("__")]

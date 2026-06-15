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
from .utils import *

def _counts_from_payload(payload: Dict[str, JSONValue]) -> Dict[str, int]:
    counts = {}
    for key in (
        "fact_count",
        "relative_count",
        "source_count",
        "conditional_relative_count",
        "relation_kind_count",
        "matched_count",
        "anchor_candidate_count",
        "matched_endpoint_count",
        "returned_count",
        "too_broad_count",
        "filter_count",
        "complete_count",
        "budget_exhausted_count",
        "depth_requested",
        "depth_used",
        "depth_max",
        "visited_function_count",
        "visited_function_budget",
        "frontier_edge_count",
        "frontier_edge_budget",
        "path_length",
        "skipped_missing_endpoint_count",
        "term_count",
        "truncated_count",
        "bytes_written",
        "uncompressed_bytes",
        "compressed_data_bytes",
        "compression_ratio_percent",
        "storage_overhead_ratio_percent",
        "read_index_bytes",
        "read_index_build_ms",
        "read_index_open_ms",
        "facts_raw_bytes",
        "facts_compressed_bytes",
        "relatives_raw_bytes",
        "relatives_compressed_bytes",
        "source_inventory_raw_bytes",
        "source_inventory_compressed_bytes",
        "limit",
        "log_write_failures",
    ):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            counts[key] = value
    return counts


def _stored_fact_line_without_payload_validation(row: Dict[str, Any]) -> StoredFactLine:
    if not isinstance(row, dict):
        raise StorageError("snapshot_corrupt", "fact line must be a JSON object")
    if row.get("schema_version") != SCHEMA_VERSION:
        raise StorageError("unsupported_schema_version", "unsupported fact line schema version")
    for field_name in ("object_id", "fact_kind", "payload", "payload_sha256"):
        if field_name not in row:
            raise StorageError("snapshot_corrupt", f"fact line missing {field_name}")
    if not isinstance(row["payload"], dict):
        raise StorageError("snapshot_corrupt", "fact line payload must be an object")
    return StoredFactLine(
        schema_version=row["schema_version"],
        object_id=row["object_id"],
        fact_kind=row["fact_kind"],
        payload=row["payload"],
        payload_sha256=row["payload_sha256"],
    )


def _stored_relative_line_without_payload_validation(row: Dict[str, Any]) -> StoredRelativeLine:
    if not isinstance(row, dict):
        raise StorageError("snapshot_corrupt", "relative line must be a JSON object")
    if row.get("schema_version") != SCHEMA_VERSION:
        raise StorageError("unsupported_schema_version", "unsupported relative line schema version")
    for field_name in (
        "relative_id",
        "from_fact_id",
        "to_fact_id",
        "relation_kind",
        "condition",
        "payload",
        "payload_sha256",
    ):
        if field_name not in row:
            raise StorageError("snapshot_corrupt", f"relative line missing {field_name}")
    if not isinstance(row["payload"], dict):
        raise StorageError("snapshot_corrupt", "relative line payload must be an object")
    return StoredRelativeLine(
        schema_version=row["schema_version"],
        relative_id=row["relative_id"],
        from_fact_id=row["from_fact_id"],
        to_fact_id=row["to_fact_id"],
        relation_kind=row["relation_kind"],
        condition=row["condition"],
        payload=row["payload"],
        payload_sha256=row["payload_sha256"],
    )


def _validated_fact_from_raw_text(raw_text: str) -> FactRecord:
    try:
        row = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise StorageError("snapshot_corrupt", "facts.jsonl contains malformed JSON") from exc
    return StoredFactLine.from_json(row).to_fact()


def _validated_relative_from_raw_text(raw_text: str) -> FactRelative:
    try:
        row = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise StorageError("snapshot_corrupt", "relatives.jsonl contains malformed JSON") from exc
    return StoredRelativeLine.from_json(row).to_relative()

def _iter_staged_facts(path: Path) -> Iterator[FactRecord]:
    for _line_number, _raw_line, raw_text in _iter_gzip_raw_lines(
        path,
        missing_message=f"{path.name} is missing",
    ):
        if raw_text:
            yield _validated_fact_from_raw_text(raw_text)


def _iter_staged_relatives(path: Path) -> Iterator[FactRelative]:
    for _line_number, _raw_line, raw_text in _iter_gzip_raw_lines(
        path,
        missing_message=f"{path.name} is missing",
    ):
        if raw_text:
            yield _validated_relative_from_raw_text(raw_text)


def _write_gzip_jsonl(path: Path, lines: Iterable[str]) -> Tuple[str, int, int]:
    digest = hashlib.sha256()
    raw_size = 0
    with path.open("wb") as raw_handle:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_handle,
            compresslevel=GZIP_COMPRESSLEVEL,
            mtime=0,
        ) as gzip_handle:
            for line_text in lines:
                encoded = line_text.encode("utf-8")
                gzip_handle.write(encoded)
                digest.update(encoded)
                raw_size += len(encoded)
    return digest.hexdigest(), raw_size, path.stat().st_size

def _validate_fact_fields(
    *,
    object_id: Any,
    object_name: Any,
    object_description: Any,
    object_source: Any,
    object_profile: Any,
    object_caller: Any,
    object_callee: Any,
    payload: Any,
) -> None:
    for field_name, value in (
        ("object_id", object_id),
        ("object_name", object_name),
        ("object_description", object_description),
        ("object_source", object_source),
        ("object_profile", object_profile),
    ):
        if not isinstance(value, str) or not value:
            raise StorageError("invalid_fact", f"{field_name} must be a non-empty string")
    for field_name, value in (("object_caller", object_caller), ("object_callee", object_callee)):
        if value is not None and not isinstance(value, str):
            raise StorageError("invalid_fact", f"{field_name} must be a string or None")
    if not isinstance(payload, dict):
        raise StorageError("invalid_fact", "payload must be a JSON object")
    _ensure_json_value(payload)


def _validate_relative_fields(
    *,
    relative_id: Any,
    from_fact_id: Any,
    to_fact_id: Any,
    relation_kind: Any,
    condition: Any,
    object_profile: Any,
    evidence_source: Any,
    confidence: Any,
    payload: Any,
) -> None:
    for field_name, value in (
        ("relative_id", relative_id),
        ("from_fact_id", from_fact_id),
        ("to_fact_id", to_fact_id),
        ("object_profile", object_profile),
        ("evidence_source", evidence_source),
    ):
        if not isinstance(value, str) or not value:
            raise StorageError("invalid_relative", f"{field_name} must be a non-empty string")
    if relation_kind not in RELATION_KINDS:
        raise StorageError("invalid_relation_kind", unsupported_relation_kind_message(relation_kind))
    if condition is not None:
        if not isinstance(condition, dict):
            raise StorageError("invalid_condition", "condition must be RelativeCondition or None")
        RelativeCondition.from_json(condition)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not math.isfinite(float(confidence)):
        raise StorageError("invalid_relative", "confidence must be a finite number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise StorageError("invalid_relative", "confidence must be between 0.0 and 1.0")
    if not isinstance(payload, dict):
        raise StorageError("invalid_relative", "payload must be a JSON object")
    _ensure_json_value(payload, code="invalid_relative")

__all__ = [name for name in globals() if not name.startswith("__")]

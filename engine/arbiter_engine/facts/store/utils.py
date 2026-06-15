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

from .constants import *

def _json_text(value: Any) -> str:
    return _canonical_json(value)


def _json_from_text(value: str, label: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        from .models import StorageError

        raise StorageError("snapshot_corrupt", f"read_index.sqlite has corrupt {label}") from exc


def _unicode_casefold_fallback(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    folded = value.casefold()
    return folded if folded != _sqlite_ascii_lower(value) else None


def _sqlite_ascii_lower(value: str) -> str:
    return value.translate(_ASCII_LOWER_TRANSLATION)

def _source_bucket(source: str) -> str:
    if ":" in source:
        prefix, suffix = source.rsplit(":", 1)
        if suffix.isdigit():
            return prefix
        return source.split(":", 1)[0]
    return source


def _endpoint_source_file(object_source: Optional[str]) -> str:
    if not object_source:
        return "<unknown-source>"
    path, separator, line = object_source.rpartition(":")
    if separator and path and line.isdigit() and int(line) > 0:
        return path
    return object_source


def _snapshot_bytes(snapshot_dir: Path) -> int:
    total = 0
    for name in (
        SNAPSHOT_DATA_FILES["facts"],
        SNAPSHOT_DATA_FILES["relatives"],
        SNAPSHOT_DATA_FILES["source_inventory"],
        READ_INDEX_FILE,
        "manifest.json",
        "stats.json",
    ):
        path = snapshot_dir / name
        if path.exists():
            total += path.stat().st_size
    return total

def _require_next_sorted_unique_id(
    previous_id: Optional[str],
    current_id: str,
    *,
    duplicate_code: str,
    unsorted_code: str,
) -> str:
    from .models import StorageError

    if previous_id is None:
        return current_id
    if current_id == previous_id:
        raise StorageError(duplicate_code, "duplicate id in sorted snapshot input")
    if current_id < previous_id:
        raise StorageError(unsorted_code, "sorted snapshot input is not ordered by id")
    return current_id

def _canonical_metadata_json(row: Dict[str, Any]) -> str:
    return _canonical_metadata_value(row)


def _canonical_metadata_value(value: Any, *, key: Optional[str] = None) -> str:
    if key in {"compression_ratio", "storage_overhead_ratio"} and isinstance(value, (int, float)) and not isinstance(value, bool):
        # This field participates in bytes_on_disk fixed-point sizing; fixed width avoids
        # small snapshots oscillating between values such as 7.8 and 7.79.
        return f"{float(value):.2f}"
    if isinstance(value, dict):
        parts = []
        for item_key in sorted(value):
            parts.append(
                f"{json.dumps(item_key, sort_keys=True, separators=(',', ':'), allow_nan=False)}:"
                f"{_canonical_metadata_value(value[item_key], key=item_key)}"
            )
        return "{" + ",".join(parts) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_canonical_metadata_value(item) for item in value) + "]"
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

def _iter_gzip_raw_lines(path: Path, *, missing_message: str) -> Iterator[Tuple[int, bytes, str]]:
    from .models import StorageError

    if not path.exists():
        raise StorageError("manifest_mismatch", missing_message, path=path)
    try:
        with gzip.open(path, "rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    yield line_number, raw_line, ""
                    continue
                try:
                    raw_text = raw_line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise StorageError("snapshot_corrupt", f"invalid gzip JSONL line {line_number}", path=path) from exc
                yield line_number, raw_line, raw_text
    except StorageError:
        raise
    except (OSError, EOFError) as exc:
        raise StorageError("snapshot_corrupt", f"{path.name} is not valid gzip JSONL", path=path) from exc


def _file_metrics(file_name: str, raw_bytes: int, compressed_bytes: int) -> Dict[str, JSONValue]:
    return {
        "file_name": file_name,
        "raw_bytes": raw_bytes,
        "compressed_bytes": compressed_bytes,
    }


def _compression_ratio(bytes_on_disk: int, uncompressed_bytes: int) -> float:
    if uncompressed_bytes <= 0:
        return 1.0
    return round(bytes_on_disk / uncompressed_bytes, 2)


def _compression_ratio_percent(bytes_on_disk: int, uncompressed_bytes: int) -> int:
    if uncompressed_bytes <= 0:
        return 100
    return round(bytes_on_disk * 100 / uncompressed_bytes)


def _is_sha256(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)

def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _ensure_line_ending(value: str) -> str:
    return value if value.endswith("\n") else value + "\n"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ensure_json_value(value: Any, *, code: str = "invalid_fact") -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        from .models import StorageError

        raise StorageError(code, "value must be JSON serializable") from exc

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _elapsed_ms(started: str) -> float:
    try:
        start = datetime.strptime(started, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - start).total_seconds() * 1000)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False

__all__ = [name for name in globals() if not name.startswith("__")]

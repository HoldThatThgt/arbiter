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

SCHEMA_VERSION = 5
SNAPSHOT_FORMAT = "compact-jsonl-gzip"
SNAPSHOT_COMPRESSION = "gzip-1"
GZIP_COMPRESSLEVEL = 1
READ_INDEX_FILE = "read_index.sqlite"
READ_INDEX_FORMAT = "sqlite-read-index"
READ_INDEX_SCHEMA_VERSION = 6
READ_INDEX_PROJECTION_KIND = "proxy-key-column-projection"
READ_INDEX_PAYLOAD_CODEC = "json-text"
READ_INDEX_SIDECARS = ("read_index.sqlite-wal", "read_index.sqlite-shm", "read_index.sqlite-journal")
SNAPSHOT_DATA_FILES = {
    "facts": "facts.jsonl.gz",
    "relatives": "relatives.jsonl.gz",
    "source_inventory": "source_inventory.jsonl.gz",
}
MAX_FACT_PAYLOAD_BYTES = 4 * 1024
MAX_RELATIVE_PAYLOAD_BYTES = 2 * 1024
MAX_CONDITION_BYTES = 1024
CURRENT_POINTER = "current"
READ_INDEX_CACHE_LIMIT = 2
SNAPSHOT_STAGING_COMMIT_INTERVAL = 1000
RELATION_KINDS = {
    "include",
    "defines",
    "declares",
    "has_field",
    "direct_call",
    "assigned_to",
    "dispatches_via",
    "field_read",
    "field_write",
}
RELATION_KIND_GUIDANCE_ORDER = (
    "include",
    "defines",
    "declares",
    "has_field",
    "direct_call",
    "assigned_to",
    "dispatches_via",
    "field_read",
    "field_write",
)
RELATION_KIND_GUIDANCE_LIST = ", ".join(RELATION_KIND_GUIDANCE_ORDER)
_RELATION_KIND_ERROR_VALUE_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def unsupported_relation_kind_message(relation_kind: object = None) -> str:
    if isinstance(relation_kind, str) and _RELATION_KIND_ERROR_VALUE_RE.fullmatch(relation_kind):
        return f"relation_kind '{relation_kind}' is not supported. Supported kinds: {RELATION_KIND_GUIDANCE_LIST}."
    return f"relation_kind is not supported. Supported kinds: {RELATION_KIND_GUIDANCE_LIST}."


CONDITION_KINDS = {"branch", "case", "loop_guard", "compile_guard", "unknown"}
RELATION_KIND_CODES = {kind: index for index, kind in enumerate(sorted(RELATION_KINDS), start=1)}
RELATION_KIND_BY_CODE = {code: kind for kind, code in RELATION_KIND_CODES.items()}
FACT_KIND_SEARCH_RANKS = {
    "type": 10,
    "function": 9,
    "global": 8,
    "macro": 7,
    "code_file": 6,
    "function_pointer_slot": 5,
    "field": 0,
}
DEFAULT_FACT_KIND_SEARCH_RANK = 4
_MISSING = object()

EXACT_NAME_SEARCH_BONUS = 10
SEARCH_EXACT_KIND_FLOOR = 3
SEARCH_CANDIDATE_MIN = 100
SEARCH_CANDIDATE_MULTIPLIER = 8
FIELD_OWNER_SEARCH_WEIGHT = 1
_FIELD_QUERY_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASCII_LOWER_TRANSLATION = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
RELATION_SEARCH_DEFINITIONS = {
    "readers": ("field", "incoming", ("field_read",)),
    "writers": ("field", "incoming", ("field_write",)),
    "accessors": ("field", "incoming", ("field_write", "field_read")),
    "callers": ("function", "incoming", ("direct_call",)),
    "callees": ("function", "outgoing", ("direct_call",)),
    "dispatches_via": ("field", "outgoing", ("assigned_to",)),
}
RELATION_SEARCH_SALIENCE_RANKS = {
    "direct_call": 0,
    "dispatches_via": 0,
    "field_write": 1,
    "field_read": 2,
    "assigned_to": 3,
}
RELATION_CLOSURE_MAX_DEPTH = 3
RELATION_REACHABLE_MAX_DEPTH = 8
RELATION_TRANSITIVE_VISITED_BUDGET = 10_000
RELATION_TRANSITIVE_FRONTIER_BUDGET = 50_000

__all__ = [name for name in globals() if not name.startswith("__")]

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
    MAX_CONDITION_BYTES,
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

SOURCE_EXTENSIONS = {".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"}
HEADER_EXTENSIONS = {".h", ".hh", ".hpp", ".hxx"}
CONTROL_WORDS = {"if", "for", "while", "switch", "return", "sizeof", "catch"}
AST_COMMAND_TIMEOUT_SECONDS = 120
AST_COMMAND_TIMEOUT_SIZE_STEP_BYTES = 1024 * 1024
AST_COMMAND_TIMEOUT_SECONDS_PER_STEP = 30
AST_COMMAND_TIMEOUT_MAX_SECONDS = 600
STREAMING_SPOOL_COMMIT_INTERVAL = 1000
MAP_REDUCE_STALE_RUN_TTL_SECONDS = 24 * 60 * 60
WORKER_RELATIVE_DEDUP_MAX_ESTIMATED_BYTES = 64 * 1024 * 1024
WORKER_RELATIVE_DEDUP_ENTRY_OVERHEAD_BYTES = 128
RELATIVE_MERGE_DEFAULT_FAN_IN = 128
RELATIVE_MERGE_MIN_FAN_IN = 2
RELATIVE_MERGE_FD_PER_SEGMENT = 2
RELATIVE_MERGE_FD_HEADROOM = 64
FIELD_ACCESS_MAX_DEPTH = 64
FIELD_ACCESS_MAX_NODES_PER_FUNCTION = 10_000
FIELD_ACCESS_WRAPPER_KINDS = {
    "BinaryOperator",
    "CallExpr",
    "CompoundAssignOperator",
    "ConditionalOperator",
    "CStyleCastExpr",
    "ImplicitCastExpr",
    "ParenExpr",
    "ReturnStmt",
    "UnaryOperator",
}
BITWISE_BINARY_OPS = {"&", "|", "^", "<<", ">>"}
BITWISE_COMPOUND_ASSIGN_OPS = {"&=", "|=", "^=", "<<=", ">>="}
COMPOUND_ASSIGN_OPS = {"*=", "/=", "%=", "+=", "-=", "<<=", ">>=", "&=", "^=", "|="}
COMPOUND_ASSIGN_KINDS = {"CompoundAssignOperator"}
INC_DEC_OPS = {"++", "--", "post++", "post--", "pre++", "pre--"}
PROBE_FUNCTION_NAME = "cipher2_toolchain_probe"
CONDITION_TARGET_KINDS = {"CallExpr", "MemberExpr"}
CONDITION_TEXT_MAX_CHARS = 512
# The serialized RelativeCondition (kind+expression+branch+source as canonical
# JSON, UTF-8) must stay at or below this ceiling or store/models.py raises a
# non-recoverable StorageError. Sourced directly from the store's authoritative
# constant so the mapper budget can never drift from the value the store
# enforces. A few bytes of headroom guard against canonical-JSON escaping
# differences when truncating the expression.
CONDITION_MAX_BYTES = MAX_CONDITION_BYTES
CONDITION_BYTES_HEADROOM = 8
HEADER_DECL_CACHE_KINDS = {
    "FunctionDecl",
    "CXXMethodDecl",
    "RecordDecl",
    "CXXRecordDecl",
    "EnumDecl",
    "TypedefDecl",
    "VarDecl",
}
_MISSING = object()

__all__ = [name for name in globals() if not name.startswith("__")]

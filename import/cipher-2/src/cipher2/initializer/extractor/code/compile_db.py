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

from cipher2.common import JSONValue
from cipher2.config import CipherConfig
from cipher2.initializer.progress import InitProgressEvent, InitProgressSink
from cipher2.storage import (
    EncodedFactLine,
    EncodedRelativeLine,
    FactRecord,
    FactRelative,
    RelativeCondition,
    SourceInventoryEntry,
    StoredFactLine,
    StoredRelativeLine,
)
from cipher2.tools.log import LogError, LogEvent, open_log

from .constants import *
from .models import *
from .mapper_utils import _hash_text, _is_cipher_path, _is_relative_to

class _MalformedCompileDatabaseError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_SPLIT_ALLOWED_OPTIONS = {
    "-I",
    "-iquote",
    "-isystem",
    "-idirafter",
    "-F",
    "-D",
    "-U",
    "-include",
    "-imacros",
    "-std",
    "-x",
    "--target",
    "-target",
    "-isysroot",
    "--sysroot",
    "-stdlib",
}
_FLAG_ALLOWED_OPTIONS = {"-nostdinc", "-nostdinc++"}
_JOINED_PREFIX_ALLOWED_OPTIONS = ("-I", "-iquote", "-isystem", "-idirafter", "-F", "-D", "-U", "-isysroot")
_JOINED_VALUE_ALLOWED_OPTIONS = ("-include", "-imacros")
_JOINED_EQUALS_ALLOWED_OPTIONS = ("-std", "--target", "-target", "--sysroot", "-stdlib")
_DROP_WITH_VALUE_OPTIONS = {
    "-o",
    "-MF",
    "-MT",
    "-MQ",
    "-MJ",
    "-Xclang",
    "-load",
    "-dependency-file",
    "-serialize-diagnostics",
    "--serialize-diagnostics",
}
_DROP_PREFIX_OPTIONS = (
    "-o",
    "-MF",
    "-MT",
    "-MQ",
    "-MJ",
    "-fplugin",
    "-load",
    "-save-temps",
    "-emit-",
    "-fprofile",
    "-fcoverage",
    "-Wl,",
    "-l",
    "-L",
    "-O",
    "-g",
)


def _compile_command_entry_from_mapping(
    target_repo: Path,
    compile_database_path: Path,
    item: Dict[str, JSONValue],
) -> Optional[_CompileCommandEntry]:
    directory_value = item.get("directory", ".")
    file_value = item.get("file")
    if not isinstance(directory_value, str) or not directory_value:
        raise _MalformedCompileDatabaseError("compile database entry directory must be a string")
    if not isinstance(file_value, str) or not file_value:
        raise _MalformedCompileDatabaseError("compile database entry file must be a string")
    raw_arguments = _entry_arguments(item)
    flags, stripped = _sanitize_compile_arguments(raw_arguments)
    database_parent = compile_database_path.parent
    directory = Path(directory_value)
    directory_path = directory if directory.is_absolute() else database_parent / directory
    source_path = Path(file_value)
    if not source_path.is_absolute():
        source_path = directory_path / source_path
    source_resolved = source_path.resolve(strict=False)
    target_resolved = Path(target_repo).resolve(strict=False)
    if not _is_relative_to(source_resolved, target_resolved) or _is_cipher_path(source_resolved, target_resolved):
        return None
    rel_source = source_resolved.relative_to(target_resolved).as_posix()
    command_hash = _hash_text(
        json.dumps(
            {
                "source": rel_source,
                "flags": flags,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return _CompileCommandEntry(
        source_path=source_resolved,
        directory_path=directory_path.resolve(strict=False),
        flags=flags,
        raw_argument_count=max(0, len(raw_arguments) - 1),
        sanitized_argument_count=len(flags),
        stripped_argument_count=stripped,
        command_hash=command_hash,
    )


def _entry_arguments(item: Dict[str, JSONValue]) -> List[str]:
    arguments = item.get("arguments")
    command = item.get("command")
    if arguments is not None:
        if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
            raise _MalformedCompileDatabaseError("compile database entry arguments must be a string list")
        if not arguments:
            raise _MalformedCompileDatabaseError("compile database entry arguments must not be empty")
        return list(arguments)
    if command is not None:
        if not isinstance(command, str) or not command:
            raise _MalformedCompileDatabaseError("compile database entry command must be a string")
        try:
            split = shlex.split(command, posix=True)
        except ValueError as exc:
            raise _MalformedCompileDatabaseError("compile database entry command cannot be shell split") from exc
        if not split:
            raise _MalformedCompileDatabaseError("compile database entry command must not be empty")
        return split
    raise _MalformedCompileDatabaseError("compile database entry requires arguments or command")


def _sanitize_compile_arguments(arguments: Sequence[str]) -> Tuple[List[str], int]:
    sanitized: List[str] = []
    stripped = 0
    args = list(arguments[1:])
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg:
            stripped += 1
            index += 1
            continue
        if arg.startswith("@"):
            stripped += 1
            index += 1
            continue
        if arg in _DROP_WITH_VALUE_OPTIONS:
            stripped += 1
            index += 1
            if index < len(args):
                stripped += 1
                index += 1
            continue
        if _is_drop_prefix_argument(arg):
            stripped += 1
            index += 1
            continue
        if arg in _FLAG_ALLOWED_OPTIONS:
            sanitized.append(arg)
            index += 1
            continue
        if arg in _SPLIT_ALLOWED_OPTIONS:
            if index + 1 >= len(args):
                raise _MalformedCompileDatabaseError(f"compile database argument {arg} requires a value")
            value = args[index + 1]
            sanitized.extend([arg, value])
            index += 2
            continue
        if _is_joined_allowed_argument(arg):
            sanitized.append(arg)
            index += 1
            continue
        stripped += 1
        index += 1
    return sanitized, stripped


def _is_drop_prefix_argument(arg: str) -> bool:
    if any(arg.startswith(prefix) for prefix in _DROP_PREFIX_OPTIONS):
        return True
    return arg in {"-c", "-S", "-E", "-M", "-MM", "-MD", "-MMD", "-shared", "-static", "-pipe"}


def _is_joined_allowed_argument(arg: str) -> bool:
    for prefix in _JOINED_EQUALS_ALLOWED_OPTIONS:
        if arg.startswith(f"{prefix}="):
            return True
    for prefix in _JOINED_PREFIX_ALLOWED_OPTIONS:
        if arg.startswith(prefix) and arg != prefix:
            return True
    for prefix in _JOINED_VALUE_ALLOWED_OPTIONS:
        if arg.startswith(prefix) and arg != prefix and not arg.startswith(f"{prefix}-"):
            return True
    return False

__all__ = [name for name in globals() if not name.startswith("__")]

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
from ._log import LogError, LogEvent, open_log

from .constants import *
from .models import *
from .mapper_utils import *

class _LibclangError(Exception):
    pass


class _LibclangUnavailableError(_LibclangError):
    def __init__(self, message: str, *, reason: str = "auto_not_found") -> None:
        super().__init__(message)
        self.reason = reason


class _LibclangVersionMismatchError(_LibclangError):
    def __init__(self, clang_version: Optional[str], libclang_version: Optional[str]) -> None:
        super().__init__("libclang version must match clang executable major version")
        self.clang_version = clang_version
        self.libclang_version = libclang_version

CX_DIAGNOSTIC_ERROR = 3
CX_DIAGNOSTIC_FATAL = 4

class _RecoverableExtractError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostic_kind: str = "unknown",
        diagnostic_reason: Optional[str] = None,
        details: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostic_kind = diagnostic_kind
        self.diagnostic_reason = diagnostic_reason or diagnostic_kind
        self.details = dict(details or {})


class _CapabilityProbeError(Exception):
    def __init__(self, message: str, *, missing_evidence: Optional[Sequence[str]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.missing_evidence = list(missing_evidence or [])


def _resolve_executable(value: Optional[str], default_name: str, unavailable_code: str) -> str:
    executable = value or default_name
    if "/" in executable or Path(executable).is_absolute():
        path = Path(executable)
        if not path.is_file() or not path.exists():
            raise _make_init_error(unavailable_code, f"{default_name} executable is unavailable")
        return str(path)
    resolved = shutil.which(executable)
    if resolved is None:
        raise _make_init_error(unavailable_code, f"{default_name} executable is unavailable")
    return resolved


def _resolve_libclang_library(clang_executable: str, configured_library: Optional[Path]) -> Tuple[str, str]:
    for candidate in _libclang_auto_candidates(clang_executable):
        if candidate.is_file() and os.access(str(candidate), os.R_OK):
            return str(candidate), "auto"
    found = ctypes.util.find_library("clang")
    if found:
        return found, "auto"
    if configured_library is not None:
        return str(_validated_configured_libclang_library(configured_library)), "configured"
    raise _LibclangUnavailableError("libclang library could not be located", reason="auto_not_found")


def _validated_configured_libclang_library(configured_library: Path) -> Path:
    candidate = Path(configured_library)
    if candidate.is_file() and os.access(str(candidate), os.R_OK):
        return candidate
    raise _LibclangUnavailableError("configured libclang library is not readable", reason="configured_unreadable")


def _libclang_auto_candidates(clang_executable: str) -> List[Path]:
    candidates: List[Path] = []
    clang_path = Path(clang_executable)
    if clang_path.is_absolute():
        prefix = clang_path.parent.parent
        for lib_dir in (prefix / "lib", prefix / "lib64", clang_path.parent):
            for name in _libclang_library_names():
                candidates.append(lib_dir / name)
    try:
        completed = subprocess.run(
            ["llvm-config", "--libdir"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        if completed.returncode == 0:
            libdir = completed.stdout.strip()
            if libdir:
                for name in _libclang_library_names():
                    candidates.append(Path(libdir) / name)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for pattern in (
        "/usr/lib/llvm-*/lib/libclang.so*",
        "/usr/local/opt/llvm/lib/libclang.dylib",
        "/opt/homebrew/opt/llvm/lib/libclang.dylib",
        "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/libclang.dylib",
    ):
        for path in glob.glob(pattern):
            candidates.append(Path(path))
    deduped: List[Path] = []
    seen: Set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return deduped


def _libclang_library_names() -> Tuple[str, ...]:
    if os.name == "nt":
        return ("libclang.dll", "clang.dll")
    if hasattr(os, "uname") and os.uname().sysname == "Darwin":
        return ("libclang.dylib",)
    return ("libclang.so", "libclang.so.1")


def _clang_major_versions_match(clang_output: str, libclang_output: str) -> bool:
    clang_major = _parse_clang_major(clang_output)
    libclang_major = _parse_clang_major(libclang_output)
    if clang_major is None or libclang_major is None:
        return True
    return clang_major == libclang_major


def _libclang_diagnostic_reason(diagnostics: Sequence[Tuple[int, Dict[str, JSONValue]]]) -> Optional[str]:
    has_error = any(severity == CX_DIAGNOSTIC_ERROR for severity, _loc in diagnostics)
    has_fatal = any(severity >= CX_DIAGNOSTIC_FATAL for severity, _loc in diagnostics)
    if has_error and has_fatal:
        return "diagnostic_error_and_fatal"
    if has_fatal:
        return "diagnostic_fatal"
    if has_error:
        return "diagnostic_error"
    return None


def _diagnostic_lines(diagnostics: Sequence[Tuple[int, Dict[str, JSONValue]]]) -> Set[int]:
    lines: Set[int] = set()
    for severity, location in diagnostics:
        if severity < CX_DIAGNOSTIC_ERROR:
            continue
        line = location.get("line")
        if isinstance(line, int):
            lines.add(line)
    return lines


def _libclang_absolute_compile_flags(compile_lookup: _CompileCommandLookup) -> List[str]:
    flags = list(compile_lookup.flags)
    if not compile_lookup.matched or compile_lookup.entry is None:
        return flags
    return _absolutize_compile_flags(flags, compile_lookup.entry.directory_path)


def _absolutize_compile_flags(flags: Sequence[str], base: Path) -> List[str]:
    result: List[str] = []
    path_flags = {"-I", "-iquote", "-isystem", "-idirafter", "-F", "-include", "-imacros", "-isysroot", "--sysroot"}
    joined_path_prefixes = ("-I", "-iquote", "-isystem", "-idirafter", "-F", "-include", "-imacros", "-isysroot")
    index = 0
    while index < len(flags):
        arg = flags[index]
        if arg in path_flags and index + 1 < len(flags):
            result.append(arg)
            result.append(_absolute_flag_path(flags[index + 1], base))
            index += 2
            continue
        joined_prefix = next(
            (prefix for prefix in joined_path_prefixes if arg.startswith(prefix) and len(arg) > len(prefix)),
            None,
        )
        if joined_prefix is not None:
            result.append(joined_prefix + _absolute_flag_path(arg[len(joined_prefix):], base))
            index += 1
            continue
        if arg.startswith("--sysroot="):
            result.append("--sysroot=" + _absolute_flag_path(arg[len("--sysroot="):], base))
            index += 1
            continue
        result.append(arg)
        index += 1
    return result


def _absolute_flag_path(value: str, base: Path) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else (base / path).resolve(strict=False))


def _tool_version_output(executable: str, tool_name: str, unavailable_code: str) -> str:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _make_init_error(unavailable_code, f"{tool_name} version probe failed") from exc
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0 or not output:
        raise _make_init_error(unavailable_code, f"{tool_name} version probe failed")
    return output


def _probe_clang_capability(executable: str, clang_args: Sequence[str]) -> ToolchainProbeResult:
    version_output = _optional_tool_version_output(executable)
    command = [
        executable,
        "-x",
        "c",
        "-Xclang",
        "-ast-dump=json",
        "-fsyntax-only",
        *clang_args,
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        header = tmp_path / "cipher2_probe_header.h"
        source = tmp_path / "cipher2_probe.c"
        header.write_text("struct cipher2_probe_record { int member; };\n", encoding="utf-8")
        source.write_text(
            '#include "cipher2_probe_header.h"\n'
            "static int cipher2_probe_callee(int value) { return value; }\n"
            f"int {PROBE_FUNCTION_NAME}(void) {{\n"
            "  struct cipher2_probe_record value;\n"
            "  value.member = 1;\n"
            "  return cipher2_probe_callee(value.member);\n"
            "}\n",
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                [*command, str(source)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=AST_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _CapabilityProbeError("clang capability probe failed") from exc
    if completed.returncode != 0:
        raise _CapabilityProbeError("clang capability probe failed")
    try:
        ast = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise _CapabilityProbeError("clang capability probe output must be valid JSON") from exc
    if not isinstance(ast, dict):
        raise _CapabilityProbeError("clang capability probe AST root must be a JSON object")
    if not _ast_contains_kind(ast, "TranslationUnitDecl"):
        raise _CapabilityProbeError("clang capability probe requires TranslationUnitDecl")
    if not _ast_contains_named_kind(ast, "FunctionDecl", PROBE_FUNCTION_NAME):
        raise _CapabilityProbeError("clang capability probe function is missing")
    loc_file_supported = _ast_has_loc_file(ast)
    call_reference_supported = _ast_has_call_reference(ast)
    member_reference_supported = _ast_has_member_reference(ast)
    qual_type_supported = _ast_has_qual_type(ast)
    missing = []
    if not loc_file_supported:
        missing.append("loc.file")
    if not call_reference_supported:
        missing.append("call_reference")
    if not member_reference_supported:
        missing.append("member_reference")
    if not qual_type_supported:
        missing.append("qualType")
    if missing:
        raise _CapabilityProbeError(
            "clang capability probe missing type-driven evidence",
            missing_evidence=missing,
        )
    vendor = _clang_vendor(version_output)
    warning_codes = [] if vendor in {"llvm", "apple"} else ["unknown_clang_vendor"]
    return ToolchainProbeResult(
        clang_executable=executable,
        clang_vendor=vendor,
        clang_version=_clang_version(version_output),
        ast_json_supported=True,
        type_driven_ast=True,
        loc_file_supported=loc_file_supported,
        call_reference_supported=call_reference_supported,
        member_reference_supported=member_reference_supported,
        qual_type_supported=qual_type_supported,
        ast_root_kind=str(ast.get("kind")) if isinstance(ast.get("kind"), str) else None,
        gcc_required=False,
        gcc_checked=False,
        warning_codes=warning_codes,
    )


def _optional_tool_version_output(executable: str) -> str:
    try:
        return _tool_version_output(executable, "clang", "clang_unavailable")
    except Exception:
        return ""


def _ast_contains_kind(node: JSONValue, kind: str) -> bool:
    if isinstance(node, dict):
        if node.get("kind") == kind:
            return True
        return any(_ast_contains_kind(value, kind) for value in node.values())
    if isinstance(node, list):
        return any(_ast_contains_kind(item, kind) for item in node)
    return False


def _ast_contains_named_kind(node: JSONValue, kind: str, name: str) -> bool:
    if isinstance(node, dict):
        if node.get("kind") == kind and node.get("name") == name:
            return True
        return any(_ast_contains_named_kind(value, kind, name) for value in node.values())
    if isinstance(node, list):
        return any(_ast_contains_named_kind(item, kind, name) for item in node)
    return False


def _ast_has_loc_file(node: JSONValue) -> bool:
    if isinstance(node, dict):
        if _node_file(node):
            return True
        return any(_ast_has_loc_file(value) for value in node.values())
    if isinstance(node, list):
        return any(_ast_has_loc_file(item) for item in node)
    return False


def _ast_has_qual_type(node: JSONValue) -> bool:
    if isinstance(node, dict):
        type_data = node.get("type")
        if isinstance(type_data, dict) and isinstance(type_data.get("qualType"), str) and type_data.get("qualType"):
            return True
        return any(_ast_has_qual_type(value) for value in node.values())
    if isinstance(node, list):
        return any(_ast_has_qual_type(item) for item in node)
    return False


def _ast_has_call_reference(node: JSONValue) -> bool:
    if isinstance(node, dict):
        if node.get("kind") == "CallExpr" and _call_reference(Path("."), node) is not None:
            return True
        return any(_ast_has_call_reference(value) for value in node.values())
    if isinstance(node, list):
        return any(_ast_has_call_reference(item) for item in node)
    return False


def _ast_has_member_reference(node: JSONValue) -> bool:
    if isinstance(node, dict):
        if node.get("kind") == "MemberExpr" and (
            _referenced_field_decl(node) is not None or _referenced_member_decl_id(node) is not None
        ):
            return True
        return any(_ast_has_member_reference(value) for value in node.values())
    if isinstance(node, list):
        return any(_ast_has_member_reference(item) for item in node)
    return False


def _clang_vendor(output: str) -> str:
    lowered = output.lower()
    if "apple clang" in lowered:
        return "apple"
    if "clang" in lowered or "llvm" in lowered:
        return "llvm"
    return "unknown"


def _clang_version(output: str) -> Optional[str]:
    match = re.search(r"\b(?:apple\s+)?clang version\s+([0-9]+(?:\.[0-9]+){0,2})", output, flags=re.IGNORECASE)
    if match is None:
        match = re.search(r"\bLLVM version\s+([0-9]+(?:\.[0-9]+){0,2})", output, flags=re.IGNORECASE)
    return match.group(1) if match is not None else None


def _parse_clang_major(output: str) -> Optional[int]:
    match = re.search(r"\bclang version\s+(\d+)(?:\.|$)", output, flags=re.IGNORECASE)
    if match is None:
        match = re.search(r"\bLLVM version\s+(\d+)(?:\.|$)", output, flags=re.IGNORECASE)
    return int(match.group(1)) if match is not None else None


def _parse_gcc_version(output: str) -> Optional[Tuple[int, int, int]]:
    match = re.search(r"\bgcc(?:\s|\s*\([^)]*\)\s+)(\d+)\.(\d+)\.(\d+)", output, flags=re.IGNORECASE)
    if match is None:
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", output)
    return tuple(int(part) for part in match.groups()) if match is not None else None


def _make_init_error(
    code: str,
    message: str,
    *,
    source: Optional[str] = None,
    details: Optional[Dict[str, JSONValue]] = None,
):
    from ._shim import InitError

    return InitError(code, message, source=source, details=details)


def _relative_source(target_repo: Path, path: Path) -> str:
    target = Path(target_repo).resolve(strict=False)
    resolved = Path(path).resolve(strict=False)
    return resolved.relative_to(target).as_posix()


def _source_id(rel_path: str, profile: str) -> str:
    return f"source:{_hash_text(profile + ':' + rel_path)[:20]}"


def _source_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".c":
        return "c_source"
    if suffix in HEADER_EXTENSIONS:
        return "header"
    return "other"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_include_paths(target_repo: Path, source: Path) -> List[str]:
    try:
        lines = source.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    target = Path(target_repo).resolve(strict=False)
    output = []
    for line in lines:
        match = re.match(r'\s*#\s*include\s+"([^"]+)"', line)
        if match is None:
            continue
        candidate = (source.parent / match.group(1)).resolve(strict=False)
        if _is_relative_to(candidate, target) and candidate.exists():
            output.append(candidate.relative_to(target).as_posix())
    return output

__all__ = [name for name in globals() if not name.startswith("__")]

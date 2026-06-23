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
from arbiter_engine.facts.log import LogError, LogEvent, open_log

from .constants import *
from .models import *
from .mapper_utils import *
from .toolchain import *
from .compile_db import *

class _CXString(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("private_flags", ctypes.c_uint)]


class _CXCursor(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_int), ("xdata", ctypes.c_int), ("data", ctypes.c_void_p * 3)]


class _CXType(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_int), ("data", ctypes.c_void_p * 2)]


class _CXSourceLocation(ctypes.Structure):
    _fields_ = [("ptr_data", ctypes.c_void_p * 2), ("int_data", ctypes.c_uint)]


class _CXSourceRange(ctypes.Structure):
    _fields_ = [("ptr_data", ctypes.c_void_p * 2), ("begin_int_data", ctypes.c_uint), ("end_int_data", ctypes.c_uint)]


class _CXToken(ctypes.Structure):
    _fields_ = [("int_data", ctypes.c_uint * 4), ("ptr_data", ctypes.c_void_p)]


@dataclass(frozen=True)
class _LibclangTranslationUnit:
    index: ctypes.c_void_p
    tu: ctypes.c_void_p
    parse_duration_ms: float


CX_CHILD_VISIT_BREAK = 0
CX_CHILD_VISIT_CONTINUE = 1
CX_CHILD_VISIT_RECURSE = 2
CX_TRANSLATION_UNIT_DETAILED_PREPROCESSING_RECORD = 0x01
CX_TRANSLATION_UNIT_INCOMPLETE = 0x02
CX_DIAGNOSTIC_ERROR = 3
CX_DIAGNOSTIC_FATAL = 4
CX_LINKAGE_INTERNAL = 2
CX_LINKAGE_EXTERNAL = 4

_LIBCLANG_KIND_NORMALIZATION = {
    "MemberRefExpr": "MemberExpr",
    "StructDecl": "RecordDecl",
    "UnionDecl": "RecordDecl",
    "ClassDecl": "CXXRecordDecl",
    "ParmDecl": "ParmVarDecl",
    "CXXMethod": "CXXMethodDecl",
    "UnexposedExpr": "ImplicitCastExpr",
}
_LIBCLANG_RECORD_TAGS = {
    "StructDecl": "struct",
    "UnionDecl": "union",
    "ClassDecl": "class",
}
_TOKEN_BINARY_OPERATORS = {
    "||",
    "&&",
    "|",
    "^",
    "&",
    "==",
    "!=",
    "<",
    ">",
    "<=",
    ">=",
    "<<",
    ">>",
    "+",
    "-",
    "*",
    "/",
    "%",
    "=",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "<<=",
    ">>=",
    "&=",
    "^=",
    "|=",
}
_TOKEN_UNARY_PREFIX_OPERATORS = {"++", "--", "*", "&", "+", "-", "!", "~"}
_TOKEN_UNARY_POSTFIX_OPERATORS = {"++", "--"}


def _normalize_libclang_cursor_kind(kind: str) -> str:
    return _LIBCLANG_KIND_NORMALIZATION.get(kind, kind)


def _operator_opcode_from_tokens(kind: str, tokens: Sequence[str]) -> Optional[str]:
    compact_tokens = [token for token in tokens if token]
    if kind == "UnaryOperator":
        if not compact_tokens:
            return None
        first = compact_tokens[0]
        if first in _TOKEN_UNARY_PREFIX_OPERATORS:
            return first
        last = compact_tokens[-1]
        if last in _TOKEN_UNARY_POSTFIX_OPERATORS:
            return f"post{last}"
        return None
    if kind not in {"BinaryOperator", "CompoundAssignOperator"}:
        return None
    depth = 0
    for token in compact_tokens:
        if token in {"(", "[", "{"}:
            depth += 1
            continue
        if token in {")", "]", "}"}:
            depth = max(0, depth - 1)
            continue
        if depth == 0 and token in _TOKEN_BINARY_OPERATORS:
            return token
    return None

class _NoopHeaderMaterializationScope:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _LibclangHeaderMaterializationScope:
    def __init__(self, backend: "_LibclangAstBackend", context: Optional[_HeaderMaterializationContext]) -> None:
        self.backend = backend
        self.context = context
        self.previous: Optional[_HeaderMaterializationContext] = None

    def __enter__(self) -> None:
        self.previous = getattr(self.backend._header_context_local, "context", None)
        self.backend._header_context_local.context = self.context
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        self.backend._header_context_local.context = self.previous
        return None


class _AstBackend:
    backend_name = "libclang"

    def probe(self) -> ToolchainProbeResult:
        raise NotImplementedError

    def load_ast(
        self,
        path: Path,
        rel_source: str,
        compile_lookup: _CompileCommandLookup,
    ) -> _AstLoadResult:
        raise NotImplementedError

    def header_materialization_context(self, context: Optional[_HeaderMaterializationContext]):
        return _NoopHeaderMaterializationScope()


class _CtypesLibclangApi:
    def __init__(self, library_path: str) -> None:
        try:
            self._lib = ctypes.CDLL(library_path)
        except OSError as exc:
            raise _LibclangUnavailableError("failed to load libclang", reason="dlopen_failed") from exc
        self.library_path = library_path
        self._callbacks: List[object] = []
        try:
            self._configure()
        except AttributeError as exc:
            raise _LibclangUnavailableError(
                "libclang library is missing a required C API symbol",
                reason="unsupported_symbol",
            ) from exc

    def _configure(self) -> None:
        lib = self._lib
        self._clang_cursor_get_binary_opcode = None
        self._clang_get_binary_operator_kind_spelling = None
        self._clang_cursor_get_unary_opcode = None
        self._clang_get_unary_operator_kind_spelling = None
        lib.clang_getCString.argtypes = [_CXString]
        lib.clang_getCString.restype = ctypes.c_char_p
        lib.clang_disposeString.argtypes = [_CXString]
        lib.clang_disposeString.restype = None
        lib.clang_getClangVersion.argtypes = []
        lib.clang_getClangVersion.restype = _CXString
        lib.clang_createIndex.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.clang_createIndex.restype = ctypes.c_void_p
        lib.clang_disposeIndex.argtypes = [ctypes.c_void_p]
        lib.clang_disposeIndex.restype = None
        lib.clang_parseTranslationUnit.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        lib.clang_parseTranslationUnit.restype = ctypes.c_void_p
        lib.clang_disposeTranslationUnit.argtypes = [ctypes.c_void_p]
        lib.clang_disposeTranslationUnit.restype = None
        lib.clang_getTranslationUnitCursor.argtypes = [ctypes.c_void_p]
        lib.clang_getTranslationUnitCursor.restype = _CXCursor
        self._visit_children_cb = ctypes.CFUNCTYPE(ctypes.c_uint, _CXCursor, _CXCursor, ctypes.c_void_p)
        lib.clang_visitChildren.argtypes = [_CXCursor, self._visit_children_cb, ctypes.c_void_p]
        lib.clang_visitChildren.restype = ctypes.c_uint
        lib.clang_getCursorKind.argtypes = [_CXCursor]
        lib.clang_getCursorKind.restype = ctypes.c_uint
        lib.clang_getCursorKindSpelling.argtypes = [ctypes.c_uint]
        lib.clang_getCursorKindSpelling.restype = _CXString
        lib.clang_getCursorSpelling.argtypes = [_CXCursor]
        lib.clang_getCursorSpelling.restype = _CXString
        lib.clang_getCursorLocation.argtypes = [_CXCursor]
        lib.clang_getCursorLocation.restype = _CXSourceLocation
        lib.clang_getCursorExtent.argtypes = [_CXCursor]
        lib.clang_getCursorExtent.restype = _CXSourceRange
        lib.clang_getRangeStart.argtypes = [_CXSourceRange]
        lib.clang_getRangeStart.restype = _CXSourceLocation
        lib.clang_getSpellingLocation.argtypes = [
            _CXSourceLocation,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.clang_getSpellingLocation.restype = None
        lib.clang_getExpansionLocation.argtypes = [
            _CXSourceLocation,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.clang_getExpansionLocation.restype = None
        lib.clang_getFileName.argtypes = [ctypes.c_void_p]
        lib.clang_getFileName.restype = _CXString
        lib.clang_getCursorType.argtypes = [_CXCursor]
        lib.clang_getCursorType.restype = _CXType
        lib.clang_getTypeSpelling.argtypes = [_CXType]
        lib.clang_getTypeSpelling.restype = _CXString
        lib.clang_getCanonicalType.argtypes = [_CXType]
        lib.clang_getCanonicalType.restype = _CXType
        lib.clang_getCursorReferenced.argtypes = [_CXCursor]
        lib.clang_getCursorReferenced.restype = _CXCursor
        lib.clang_getCursorDefinition.argtypes = [_CXCursor]
        lib.clang_getCursorDefinition.restype = _CXCursor
        lib.clang_getCursorUSR.argtypes = [_CXCursor]
        lib.clang_getCursorUSR.restype = _CXString
        lib.clang_getCursorSemanticParent.argtypes = [_CXCursor]
        lib.clang_getCursorSemanticParent.restype = _CXCursor
        lib.clang_isCursorDefinition.argtypes = [_CXCursor]
        lib.clang_isCursorDefinition.restype = ctypes.c_uint
        lib.clang_getCursorLinkage.argtypes = [_CXCursor]
        lib.clang_getCursorLinkage.restype = ctypes.c_uint
        self._clang_cursor_get_binary_opcode = self._optional_function(
            "clang_Cursor_getBinaryOpcode",
            [_CXCursor],
            ctypes.c_uint,
        )
        self._clang_get_binary_operator_kind_spelling = self._optional_function(
            "clang_getBinaryOperatorKindSpelling",
            [ctypes.c_uint],
            _CXString,
        )
        self._clang_cursor_get_unary_opcode = self._optional_function(
            "clang_Cursor_getUnaryOpcode",
            [_CXCursor],
            ctypes.c_uint,
        )
        self._clang_get_unary_operator_kind_spelling = self._optional_function(
            "clang_getUnaryOperatorKindSpelling",
            [ctypes.c_uint],
            _CXString,
        )
        lib.clang_Cursor_isNull.argtypes = [_CXCursor]
        lib.clang_Cursor_isNull.restype = ctypes.c_int
        lib.clang_getNumDiagnostics.argtypes = [ctypes.c_void_p]
        lib.clang_getNumDiagnostics.restype = ctypes.c_uint
        lib.clang_getDiagnostic.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        lib.clang_getDiagnostic.restype = ctypes.c_void_p
        lib.clang_disposeDiagnostic.argtypes = [ctypes.c_void_p]
        lib.clang_disposeDiagnostic.restype = None
        lib.clang_getDiagnosticSeverity.argtypes = [ctypes.c_void_p]
        lib.clang_getDiagnosticSeverity.restype = ctypes.c_uint
        lib.clang_getDiagnosticLocation.argtypes = [ctypes.c_void_p]
        lib.clang_getDiagnosticLocation.restype = _CXSourceLocation
        lib.clang_tokenize.argtypes = [
            ctypes.c_void_p,
            _CXSourceRange,
            ctypes.POINTER(ctypes.POINTER(_CXToken)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.clang_tokenize.restype = None
        lib.clang_disposeTokens.argtypes = [ctypes.c_void_p, ctypes.POINTER(_CXToken), ctypes.c_uint]
        lib.clang_disposeTokens.restype = None
        lib.clang_getTokenSpelling.argtypes = [ctypes.c_void_p, _CXToken]
        lib.clang_getTokenSpelling.restype = _CXString

    def close(self) -> None:
        # The dlopen handle is released at process exit. There is no stdlib-public dlclose
        # (only the private _ctypes.dlclose, which the engine's stdlib-only import policy
        # forbids), so releasing it eagerly isn't possible here; libclang's native mapping
        # therefore stays resident for the process lifetime after a version-mismatch retry
        # (a bounded, one-time leak). Kept as a no-op for call-site symmetry.
        return

    def _optional_function(
        self,
        name: str,
        argtypes: Sequence[object],
        restype: object,
    ) -> Optional[object]:
        try:
            function = getattr(self._lib, name)
        except AttributeError:
            return None
        function.argtypes = list(argtypes)
        function.restype = restype
        return function

    def string(self, value: _CXString) -> str:
        raw = self._lib.clang_getCString(value)
        text = raw.decode("utf-8", "replace") if raw else ""
        self._lib.clang_disposeString(value)
        return text

    def version(self) -> str:
        return self.string(self._lib.clang_getClangVersion())

    def cursor_kind(self, cursor: _CXCursor) -> str:
        kind = int(self._lib.clang_getCursorKind(cursor))
        return self.string(self._lib.clang_getCursorKindSpelling(kind))

    def cursor_spelling(self, cursor: _CXCursor) -> str:
        return self.string(self._lib.clang_getCursorSpelling(cursor))

    def cursor_usr(self, cursor: _CXCursor) -> str:
        return self.string(self._lib.clang_getCursorUSR(cursor))

    def cursor_type_spelling(self, cursor: _CXCursor) -> Tuple[Optional[str], Optional[str]]:
        ctype = self._lib.clang_getCursorType(cursor)
        qual = self.string(self._lib.clang_getTypeSpelling(ctype))
        canonical = self.string(self._lib.clang_getTypeSpelling(self._lib.clang_getCanonicalType(ctype)))
        return (qual or None, canonical or None)

    def cursor_location(self, cursor: _CXCursor) -> Dict[str, JSONValue]:
        return self._location_to_json(self._lib.clang_getCursorLocation(cursor), spelling=True)

    def cursor_range_begin(self, cursor: _CXCursor) -> Dict[str, JSONValue]:
        extent = self._lib.clang_getCursorExtent(cursor)
        return self._location_to_json(self._lib.clang_getRangeStart(extent), spelling=True)

    def _location_to_json(self, location: _CXSourceLocation, *, spelling: bool) -> Dict[str, JSONValue]:
        file_obj = ctypes.c_void_p()
        line = ctypes.c_uint()
        column = ctypes.c_uint()
        offset = ctypes.c_uint()
        if spelling:
            self._lib.clang_getSpellingLocation(
                location,
                ctypes.byref(file_obj),
                ctypes.byref(line),
                ctypes.byref(column),
                ctypes.byref(offset),
            )
        else:
            self._lib.clang_getExpansionLocation(
                location,
                ctypes.byref(file_obj),
                ctypes.byref(line),
                ctypes.byref(column),
                ctypes.byref(offset),
            )
        result: Dict[str, JSONValue] = {}
        if file_obj.value:
            filename = self.string(self._lib.clang_getFileName(file_obj))
            if filename:
                result["file"] = filename
        if line.value:
            result["line"] = int(line.value)
        if column.value:
            result["col"] = int(column.value)
        if offset.value:
            result["offset"] = int(offset.value)
        return result

    def children(self, cursor: _CXCursor) -> List[_CXCursor]:
        children: List[_CXCursor] = []

        def visitor(child: _CXCursor, _parent: _CXCursor, _data: ctypes.c_void_p) -> int:
            children.append(child)
            return CX_CHILD_VISIT_CONTINUE

        callback = self._visit_children_cb(visitor)
        self._callbacks.append(callback)
        try:
            self._lib.clang_visitChildren(cursor, callback, None)
        finally:
            self._callbacks.pop()
        return children

    def referenced(self, cursor: _CXCursor) -> Optional[_CXCursor]:
        referenced = self._lib.clang_getCursorReferenced(cursor)
        if self._lib.clang_Cursor_isNull(referenced):
            return None
        return referenced

    def semantic_parent(self, cursor: _CXCursor) -> Optional[_CXCursor]:
        parent = self._lib.clang_getCursorSemanticParent(cursor)
        if self._lib.clang_Cursor_isNull(parent):
            return None
        return parent

    def parse_translation_unit(self, source: Path, args: Sequence[str]) -> _LibclangTranslationUnit:
        index = self._lib.clang_createIndex(0, 0)
        if not index:
            raise _LibclangError("failed to create libclang index")
        encoded_args = [arg.encode("utf-8") for arg in args]
        arg_array = (ctypes.c_char_p * len(encoded_args))(*encoded_args) if encoded_args else None
        started = time.perf_counter()
        tu = self._lib.clang_parseTranslationUnit(
            index,
            str(source).encode("utf-8"),
            arg_array,
            len(encoded_args),
            None,
            0,
            CX_TRANSLATION_UNIT_DETAILED_PREPROCESSING_RECORD | CX_TRANSLATION_UNIT_INCOMPLETE,
        )
        duration_ms = _elapsed_ms(started)
        if not tu:
            self._lib.clang_disposeIndex(index)
            raise _LibclangError("libclang parse returned no translation unit")
        return _LibclangTranslationUnit(index=index, tu=tu, parse_duration_ms=duration_ms)

    def dispose_translation_unit(self, tu: _LibclangTranslationUnit) -> None:
        self._lib.clang_disposeTranslationUnit(tu.tu)
        self._lib.clang_disposeIndex(tu.index)

    def translation_unit_cursor(self, tu: ctypes.c_void_p) -> _CXCursor:
        return self._lib.clang_getTranslationUnitCursor(tu)

    def diagnostics(self, tu: ctypes.c_void_p) -> List[Tuple[int, Dict[str, JSONValue]]]:
        diagnostics: List[Tuple[int, Dict[str, JSONValue]]] = []
        count = int(self._lib.clang_getNumDiagnostics(tu))
        for index in range(count):
            diagnostic = self._lib.clang_getDiagnostic(tu, index)
            if not diagnostic:
                continue
            try:
                severity = int(self._lib.clang_getDiagnosticSeverity(diagnostic))
                loc = self._location_to_json(self._lib.clang_getDiagnosticLocation(diagnostic), spelling=False)
                diagnostics.append((severity, loc))
            finally:
                self._lib.clang_disposeDiagnostic(diagnostic)
        return diagnostics

    def cursor_tokens(self, tu: ctypes.c_void_p, cursor: _CXCursor) -> List[str]:
        tokens = ctypes.POINTER(_CXToken)()
        count = ctypes.c_uint()
        extent = self._lib.clang_getCursorExtent(cursor)
        self._lib.clang_tokenize(tu, extent, ctypes.byref(tokens), ctypes.byref(count))
        if not tokens or count.value == 0:
            return []
        try:
            return [
                self.string(self._lib.clang_getTokenSpelling(tu, tokens[index]))
                for index in range(int(count.value))
            ]
        finally:
            self._lib.clang_disposeTokens(tu, tokens, count)

    def cursor_is_definition(self, cursor: _CXCursor) -> bool:
        return bool(self._lib.clang_isCursorDefinition(cursor))

    def cursor_linkage(self, cursor: _CXCursor) -> Optional[str]:
        linkage = int(self._lib.clang_getCursorLinkage(cursor))
        if linkage == CX_LINKAGE_INTERNAL:
            return "internal"
        if linkage == CX_LINKAGE_EXTERNAL:
            return "external"
        return None

    def cursor_binary_opcode(self, cursor: _CXCursor) -> Optional[str]:
        if self._clang_cursor_get_binary_opcode is None or self._clang_get_binary_operator_kind_spelling is None:
            return None
        opcode = int(self._clang_cursor_get_binary_opcode(cursor))
        if opcode <= 0:
            return None
        return self.string(self._clang_get_binary_operator_kind_spelling(opcode)) or None

    def cursor_unary_opcode(self, cursor: _CXCursor) -> Optional[str]:
        if self._clang_cursor_get_unary_opcode is None or self._clang_get_unary_operator_kind_spelling is None:
            return None
        opcode = int(self._clang_cursor_get_unary_opcode(cursor))
        if opcode <= 0:
            return None
        return self.string(self._clang_get_unary_operator_kind_spelling(opcode)) or None


class _LibclangAstBackend(_AstBackend):
    backend_name = "libclang"

    def __init__(
        self,
        *,
        clang_executable: str,
        clang_args: Sequence[str],
        target_repo: Path,
        configured_library: Optional[Path],
    ) -> None:
        self.clang_executable = clang_executable
        self.clang_args = list(clang_args)
        self.target_repo = Path(target_repo)
        self._target_repo_resolved = self.target_repo.resolve(strict=False)
        self._repo_relative_source_cache = _RepoRelativeSourceCache()
        self.clang_version_output = _tool_version_output(clang_executable, "clang", "clang_unavailable")
        self.library_path, self.library_scope = _resolve_libclang_library(clang_executable, configured_library)
        self._header_context_local = threading.local()
        try:
            self.api, self.libclang_version = self._load_matching_api(self.library_path)
        except (_LibclangUnavailableError, _LibclangVersionMismatchError):
            if configured_library is None or self.library_scope == "configured":
                raise
            configured_path = _validated_configured_libclang_library(configured_library)
            self.library_path = str(configured_path)
            self.library_scope = "configured"
            self.api, self.libclang_version = self._load_matching_api(self.library_path)

    def _load_matching_api(self, library_path: str) -> Tuple[_CtypesLibclangApi, str]:
        api = _CtypesLibclangApi(library_path)
        try:
            libclang_version = api.version()
            if not _clang_major_versions_match(self.clang_version_output, libclang_version):
                raise _LibclangVersionMismatchError(
                    _clang_version(self.clang_version_output), _clang_version(libclang_version)
                )
        except BaseException:
            # The caller drops this api on any failure here; release its dlopen handle so a
            # subsequent fallback load does not leak the first library for the process lifetime.
            close = getattr(api, "close", None)
            if callable(close):
                close()
            raise
        return api, libclang_version

    def header_materialization_context(self, context: Optional[_HeaderMaterializationContext]):
        return _LibclangHeaderMaterializationScope(self, context)

    def probe(self) -> ToolchainProbeResult:
        with tempfile.NamedTemporaryFile("w", suffix=".c", encoding="utf-8", delete=False) as handle:
            handle.write(
                "struct cipher2_probe_record { int member; };\n"
                "int cipher2_probe_callee(int value) { return value; }\n"
                "int cipher2_toolchain_probe(void) { struct cipher2_probe_record record; return cipher2_probe_callee(record.member); }\n"
            )
            probe_path = Path(handle.name)
        try:
            load = self._load_ast_for_path(probe_path, [*self.clang_args, "-x", "c", "-ferror-limit=0"])
        finally:
            try:
                probe_path.unlink()
            except OSError:
                pass
        ast = load.ast
        missing: List[str] = []
        if not _ast_contains_named_kind(ast, "FunctionDecl", PROBE_FUNCTION_NAME):
            missing.append("probe_function")
        if not _ast_has_loc_file(ast):
            missing.append("loc.file")
        if not _ast_has_call_reference(ast):
            missing.append("call_reference")
        if not _ast_has_member_reference(ast):
            missing.append("member_reference")
        if not _ast_has_qual_type(ast):
            missing.append("qualType")
        if missing:
            raise _CapabilityProbeError("clang AST is missing required type-driven evidence", missing_evidence=missing)
        vendor = _clang_vendor(self.clang_version_output)
        warning_codes = [] if vendor in {"llvm", "apple"} else ["unknown_clang_vendor"]
        return ToolchainProbeResult(
            clang_executable=self.clang_executable,
            clang_vendor=vendor,
            clang_version=_clang_version(self.clang_version_output),
            ast_json_supported=False,
            type_driven_ast=True,
            loc_file_supported=True,
            call_reference_supported=True,
            member_reference_supported=True,
            qual_type_supported=True,
            ast_root_kind=str(ast.get("kind")) if isinstance(ast.get("kind"), str) else None,
            gcc_required=False,
            gcc_checked=False,
            warning_codes=warning_codes,
            backend=self.backend_name,
            libclang_library=self.library_path,
            libclang_library_scope=self.library_scope,
            libclang_version=_clang_version(self.libclang_version),
            version_match=True,
        )

    def load_ast(
        self,
        path: Path,
        rel_source: str,
        compile_lookup: _CompileCommandLookup,
    ) -> _AstLoadResult:
        args = [
            *self.clang_args,
            *_libclang_absolute_compile_flags(compile_lookup),
            "-ferror-limit=0",
        ]
        return self._load_ast_for_path(path, args)

    def _load_ast_for_path(self, path: Path, args: Sequence[str]) -> _AstLoadResult:
        tu = None
        try:
            tu = self.api.parse_translation_unit(path, args)
            diagnostics = self.api.diagnostics(tu.tu)
            diagnostic_reason = _libclang_diagnostic_reason(diagnostics)
            prune_root_resolved = self._cursor_prune_root_for_path(path)
            root = self._cursor_to_ast(
                self.api.translation_unit_cursor(tu.tu),
                None,
                diagnostic_lines_by_file=_diagnostic_lines(diagnostics),
                translation_unit=tu.tu,
                _prune_root_resolved=prune_root_resolved,
            )
            if root.get("kind") != "TranslationUnitDecl":
                root["kind"] = "TranslationUnitDecl"
            if not isinstance(root.get("inner"), list) or not root.get("inner"):
                raise _RecoverableExtractError(
                    "clang_ast_failed",
                    "libclang AST TranslationUnitDecl must contain nodes",
                    diagnostic_kind="malformed_ast",
                    diagnostic_reason="parse_failed",
                )
            return _AstLoadResult(
                ast=root,
                diagnostic_kind="partial_ast" if diagnostic_reason is not None else "ok",
                diagnostic_reason=diagnostic_reason or "ok",
                partial=diagnostic_reason is not None,
                warning_code="clang_ast_partial" if diagnostic_reason is not None else None,
                backend=self.backend_name,
                parse_duration_ms=tu.parse_duration_ms,
            )
        except _RecoverableExtractError:
            raise
        except _LibclangError as exc:
            raise _RecoverableExtractError(
                "clang_ast_failed",
                str(exc),
                diagnostic_kind="libclang_error",
                diagnostic_reason="libclang_error",
            ) from exc
        finally:
            if tu is not None:
                self.api.dispose_translation_unit(tu)

    def _cursor_to_ast(
        self,
        cursor: _CXCursor,
        parent: Optional[_CXCursor],
        *,
        diagnostic_lines_by_file: Dict[str, Set[int]],
        translation_unit: Optional[ctypes.c_void_p] = None,
        _precomputed_loc: Optional[Dict[str, JSONValue]] = None,
        _precomputed_range_begin: Optional[Dict[str, JSONValue]] = None,
        _prune_root_resolved: Optional[Path] = None,
        _header_materialization_shallow: bool = False,
    ) -> Dict[str, JSONValue]:
        native_kind = self.api.cursor_kind(cursor) or "UnexposedDecl"
        kind = _normalize_libclang_cursor_kind(native_kind)
        name = self.api.cursor_spelling(cursor)
        node: Dict[str, JSONValue] = {"kind": kind}
        if native_kind != kind:
            node["libclangKind"] = native_kind
        tag_used = _LIBCLANG_RECORD_TAGS.get(native_kind)
        if tag_used is not None:
            node["tagUsed"] = tag_used
        if name:
            node["name"] = name
        loc = self.api.cursor_location(cursor) if _precomputed_loc is None else _precomputed_loc
        if loc:
            node["loc"] = loc
            line = loc.get("line")
            file_value = loc.get("file")
            if (
                isinstance(line, int)
                and isinstance(file_value, str)
                and line in diagnostic_lines_by_file.get(file_value, ())
            ):
                node["containsErrors"] = True
        range_begin = self.api.cursor_range_begin(cursor) if _precomputed_range_begin is None else _precomputed_range_begin
        if range_begin:
            node["range"] = {"begin": range_begin}
        qual_type, canonical_type = self.api.cursor_type_spelling(cursor)
        if qual_type:
            type_payload: Dict[str, JSONValue] = {"qualType": qual_type}
            if canonical_type and canonical_type != qual_type:
                type_payload["desugaredQualType"] = canonical_type
            node["type"] = type_payload
        usr = self.api.cursor_usr(cursor)
        if usr:
            node["id"] = usr
        elif name and loc:
            node["id"] = _hash_text(json.dumps({"kind": kind, "name": name, "loc": loc}, sort_keys=True))[:24]
        if self.api.cursor_is_definition(cursor):
            node["isThisDeclarationADefinition"] = True
        linkage = self.api.cursor_linkage(cursor)
        if linkage is not None:
            node["linkage"] = linkage
        if kind in {"BinaryOperator", "CompoundAssignOperator"}:
            opcode = self.api.cursor_binary_opcode(cursor) or self._cursor_token_opcode(translation_unit, cursor, kind)
            if opcode:
                node["opcode"] = opcode
        elif kind == "UnaryOperator":
            opcode = self.api.cursor_unary_opcode(cursor) or self._cursor_token_opcode(translation_unit, cursor, kind)
            if opcode:
                node["opcode"] = opcode
        semantic_parent = self.api.semantic_parent(cursor)
        if semantic_parent is not None:
            parent_kind = _normalize_libclang_cursor_kind(self.api.cursor_kind(semantic_parent))
            parent_name = self.api.cursor_spelling(semantic_parent)
            if parent_kind in {"FunctionDecl", "CXXMethodDecl"}:
                node["isLocal"] = True
                if kind == "VarDecl":
                    node["storageClass"] = "auto"
            if parent_name and parent_kind in {"RecordDecl", "CXXRecordDecl"}:
                node["ownerName"] = parent_name
        referenced = self.api.referenced(cursor)
        if referenced is not None and kind not in {
            "FunctionDecl",
            "CXXMethodDecl",
            "VarDecl",
            "ParmVarDecl",
            "RecordDecl",
            "CXXRecordDecl",
            "FieldDecl",
            "TypedefDecl",
        }:
            referenced_node = self._referenced_decl_to_json(referenced)
            if referenced_node:
                if referenced_node.get("kind") in {"FieldDecl", "IndirectFieldDecl"}:
                    ref_id = referenced_node.get("id")
                    if isinstance(ref_id, str):
                        node["referencedMemberDecl"] = ref_id
                node["referencedDecl"] = referenced_node
        if _header_materialization_shallow:
            node["cipher2HeaderCacheHit"] = True
            return node
        children = []
        for child in self.api.children(cursor):
            child_loc = self.api.cursor_location(child)
            child_range_begin = self.api.cursor_range_begin(child)
            if not self._cursor_is_prune_scope_scoped(child_loc, child_range_begin, _prune_root_resolved):
                continue
            header_context = self._header_materialization_context()
            header_key = self._cursor_header_materialization_key(child, child_loc, child_range_begin, header_context)
            if header_context is not None and header_key is not None:
                if header_context.cache.is_materialized(header_key, header_context):
                    header_context.stats.header_decl_cache_hit_count += 1
                    header_context.stats.header_decl_skipped_subtree_count += 1
                    children.append(self._cursor_to_ast(
                        child,
                        cursor,
                        diagnostic_lines_by_file=diagnostic_lines_by_file,
                        translation_unit=translation_unit,
                        _precomputed_loc=child_loc,
                        _precomputed_range_begin=child_range_begin,
                        _prune_root_resolved=_prune_root_resolved,
                        _header_materialization_shallow=True,
                    ))
                    continue
                header_context.stats.header_decl_cache_miss_count += 1
            children.append(self._cursor_to_ast(
                child,
                cursor,
                diagnostic_lines_by_file=diagnostic_lines_by_file,
                translation_unit=translation_unit,
                _precomputed_loc=child_loc,
                _precomputed_range_begin=child_range_begin,
                _prune_root_resolved=_prune_root_resolved,
            ))
        if children:
            node["inner"] = children
        return node

    def _header_materialization_context(self) -> Optional[_HeaderMaterializationContext]:
        local = getattr(self, "_header_context_local", None)
        if local is None:
            return None
        return getattr(local, "context", None)

    def _cursor_header_materialization_key(
        self,
        cursor: _CXCursor,
        loc: Dict[str, JSONValue],
        range_begin: Dict[str, JSONValue],
        context: Optional[_HeaderMaterializationContext],
    ) -> Optional[str]:
        if context is None:
            return None
        native_kind = self.api.cursor_kind(cursor) or "UnexposedDecl"
        kind = _normalize_libclang_cursor_kind(native_kind)
        if kind not in HEADER_DECL_CACHE_KINDS:
            return None
        canonical_source = self._cursor_repo_relative_source(loc, range_begin)
        if canonical_source is None or canonical_source == context.rel_source:
            return None
        name = self.api.cursor_spelling(cursor)
        usr = self.api.cursor_usr(cursor)
        linkage = self.api.cursor_linkage(cursor) or "unknown"
        identity = {
            "kind": kind,
            "usr": usr or None,
            "name": name or None,
            "canonical_source": canonical_source,
            "line": _location_int(loc, "line") or _location_int(range_begin, "line"),
            "column": _location_int(loc, "col") or _location_int(range_begin, "col"),
            "range_line": _location_int(range_begin, "line"),
            "range_column": _location_int(range_begin, "col"),
            "linkage": linkage,
            "tag_used": _LIBCLANG_RECORD_TAGS.get(native_kind),
            "context": context.context_hash,
        }
        return _hash_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))

    def _cursor_repo_relative_source(
        self,
        loc: Dict[str, JSONValue],
        range_begin: Dict[str, JSONValue],
    ) -> Optional[str]:
        cache = getattr(self, "_repo_relative_source_cache", None)
        if cache is None:
            cache = _RepoRelativeSourceCache()
            self._repo_relative_source_cache = cache
        for location in (loc, range_begin):
            file_value = location.get("file") if isinstance(location, dict) else None
            source = _repo_relative_source_from_file_value(
                self.target_repo,
                self.target_repo,
                file_value,
                cache=cache,
            )
            if source is not None:
                return source
        return None

    def _cursor_prune_root_for_path(self, path: Path) -> Path:
        target_resolved = getattr(self, "_target_repo_resolved", None)
        if target_resolved is None:
            target_resolved = Path(getattr(self, "target_repo")).resolve(strict=False)
        source_resolved = Path(path).resolve(strict=False)
        if _is_relative_to(source_resolved, target_resolved):
            return target_resolved
        return source_resolved.parent

    def _cursor_is_prune_scope_scoped(
        self,
        loc: Dict[str, JSONValue],
        range_begin: Dict[str, JSONValue],
        prune_root_resolved: Optional[Path],
    ) -> bool:
        if prune_root_resolved is None:
            prune_root_resolved = getattr(self, "_target_repo_resolved", None)
        if prune_root_resolved is None:
            target_repo = getattr(self, "target_repo", None)
            if target_repo is None:
                return True
            prune_root_resolved = Path(target_repo).resolve(strict=False)
        saw_file = False
        for location in (loc, range_begin):
            file_value = location.get("file") if isinstance(location, dict) else None
            if not isinstance(file_value, str) or not file_value:
                continue
            saw_file = True
            candidate = Path(file_value)
            if not candidate.is_absolute():
                candidate = prune_root_resolved / candidate
            resolved = candidate.resolve(strict=False)
            if _is_relative_to(resolved, prune_root_resolved) and not _is_cipher_path(resolved, prune_root_resolved):
                return True
        return not saw_file

    def _cursor_token_opcode(
        self,
        translation_unit: Optional[ctypes.c_void_p],
        cursor: _CXCursor,
        kind: str,
    ) -> Optional[str]:
        if translation_unit is None:
            return None
        return _operator_opcode_from_tokens(kind, self.api.cursor_tokens(translation_unit, cursor))

    def _referenced_decl_to_json(self, cursor: _CXCursor) -> Dict[str, JSONValue]:
        native_kind = self.api.cursor_kind(cursor) or "Decl"
        kind = _normalize_libclang_cursor_kind(native_kind)
        name = self.api.cursor_spelling(cursor)
        node: Dict[str, JSONValue] = {"kind": kind}
        if native_kind != kind:
            node["libclangKind"] = native_kind
        tag_used = _LIBCLANG_RECORD_TAGS.get(native_kind)
        if tag_used is not None:
            node["tagUsed"] = tag_used
        if name:
            node["name"] = name
        loc = self.api.cursor_location(cursor)
        if loc:
            node["loc"] = loc
        usr = self.api.cursor_usr(cursor)
        if usr:
            node["id"] = usr
        qual_type, canonical_type = self.api.cursor_type_spelling(cursor)
        if qual_type:
            type_payload: Dict[str, JSONValue] = {"qualType": qual_type}
            if canonical_type and canonical_type != qual_type:
                type_payload["desugaredQualType"] = canonical_type
            node["type"] = type_payload
        parent = self.api.semantic_parent(cursor)
        if parent is not None:
            parent_name = self.api.cursor_spelling(parent)
            if parent_name:
                node["ownerName"] = parent_name
        linkage = self.api.cursor_linkage(cursor)
        if linkage is not None:
            node["linkage"] = linkage
        return node


class _JsonSubprocessTestBackend(_AstBackend):
    backend_name = "libclang"

    def __init__(self, extractor: "CodeFactExtractor", clang_executable: str) -> None:
        self.extractor = extractor
        self.clang_executable = clang_executable

    def probe(self) -> ToolchainProbeResult:
        result = _probe_clang_capability(self.clang_executable, self.extractor.config.clang_args)
        return replace(
            result,
            backend="libclang",
            ast_json_supported=False,
            libclang_library="test-json-backend",
            libclang_library_scope="test",
            libclang_version=result.clang_version,
            version_match=True,
        )

    def load_ast(
        self,
        path: Path,
        rel_source: str,
        compile_lookup: _CompileCommandLookup,
    ) -> _AstLoadResult:
        load = self.extractor._load_ast_json_for_test(path, rel_source, compile_lookup)
        return replace(load, backend=self.backend_name)


class _InMemoryProcessAstBackend(_AstBackend):
    backend_name = "libclang"

    def __init__(self, ast: Dict[str, JSONValue]) -> None:
        self._ast = ast

    def probe(self) -> ToolchainProbeResult:
        raise AssertionError("in-memory process backend is installed after probe")

    def load_ast(
        self,
        path: Path,
        rel_source: str,
        compile_lookup: _CompileCommandLookup,
    ) -> _AstLoadResult:
        if rel_source in self._ast and isinstance(self._ast[rel_source], dict):
            return _AstLoadResult(ast=self._ast[rel_source])
        return _AstLoadResult(ast=self._ast)


_TEST_AST_BACKEND_FACTORY: Optional[Callable[["CodeFactExtractor", str], _AstBackend]] = None


def _install_json_test_libclang_backend() -> None:
    global _TEST_AST_BACKEND_FACTORY
    _TEST_AST_BACKEND_FACTORY = lambda extractor, clang: _JsonSubprocessTestBackend(extractor, clang)


def _clear_test_libclang_backend() -> None:
    global _TEST_AST_BACKEND_FACTORY
    _TEST_AST_BACKEND_FACTORY = None

__all__ = [name for name in globals() if not name.startswith("__")]

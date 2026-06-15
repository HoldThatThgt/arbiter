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
from arbiter_engine.facts.store._common import LogError, LogEvent, open_log

from .constants import *
from .models import *

class _RepoRelativeSourceCache:
    def __init__(self) -> None:
        self._target_resolved_by_key: Dict[str, Path] = {}
        self._source_by_key: Dict[Tuple[str, str, str], Optional[str]] = {}

    def source_from_file_value(
        self,
        target_repo: Path,
        source_location_base: Path,
        file_value: Optional[str],
    ) -> Optional[str]:
        if not isinstance(file_value, str) or not file_value:
            return None
        target_path = Path(target_repo)
        source_base_path = Path(source_location_base)
        target_key = os.fspath(target_path)
        source_base_key = os.fspath(source_base_path)
        key = (target_key, source_base_key, file_value)
        if key in self._source_by_key:
            return self._source_by_key[key]
        target_resolved = self._target_resolved_by_key.get(target_key)
        if target_resolved is None:
            target_resolved = target_path.resolve(strict=False)
            self._target_resolved_by_key[target_key] = target_resolved
        source = _repo_relative_source_from_file_value_uncached(
            target_path,
            source_base_path,
            file_value,
            target_resolved=target_resolved,
        )
        self._source_by_key[key] = source
        return source

    def entry_count(self) -> int:
        return len(self._source_by_key)

def _dict_children(node: Dict[str, JSONValue]) -> List[Dict[str, JSONValue]]:
    return [child for child in _node_children(node) if isinstance(child, dict)]


def _compile_guard_conditions_by_line(target_repo: Path, rel_source: str) -> Dict[int, _ConditionAnnotation]:
    source_path = target_repo / rel_source
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    guards_by_line: Dict[int, _ConditionAnnotation] = {}
    stack: List[_ConditionAnnotation] = []
    for line_number, raw_line in enumerate(lines, 1):
        directive = _preprocessor_condition_directive(raw_line)
        if directive is not None:
            name, expression, branch = directive
            if name in {"if", "ifdef", "ifndef"}:
                stack.append(
                    _ConditionAnnotation(
                        kind="compile_guard",
                        expression=_compact_condition_text(expression),
                        branch=branch,
                        source=f"{rel_source}:{line_number}",
                    )
                )
            elif name in {"elif", "else"} and stack:
                stack[-1] = _ConditionAnnotation(
                    kind="compile_guard",
                    expression=_compact_condition_text(expression),
                    branch=branch,
                    source=f"{rel_source}:{line_number}",
                )
            elif name == "endif" and stack:
                stack.pop()
            continue
        if stack:
            guards_by_line[line_number] = stack[-1]
    return guards_by_line


def _preprocessor_condition_directive(raw_line: str) -> Optional[Tuple[str, str, str]]:
    match = re.match(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$", raw_line)
    if match is None:
        return None
    directive = match.group(1)
    rest = match.group(2).strip()
    if directive == "ifdef":
        return directive, f"defined({rest})" if rest else "defined(<unknown>)", "then"
    if directive == "ifndef":
        return directive, f"!defined({rest})" if rest else "!defined(<unknown>)", "then"
    if directive == "if":
        return directive, rest or "<unknown>", "then"
    if directive == "elif":
        return directive, rest or "<unknown>", "elif"
    if directive == "else":
        return directive, "else", "else"
    return directive, "", ""


def _render_condition_expression(node: Optional[Dict[str, JSONValue]]) -> Optional[str]:
    if node is None:
        return None
    kind = node.get("kind")
    children = _dict_children(node)
    if kind in {"ImplicitCastExpr", "CStyleCastExpr", "ExprWithCleanups", "FullExpr", "MaterializeTemporaryExpr", "ConstantExpr"}:
        return _render_condition_expression(children[-1]) if children else _expr_name(node)
    if kind == "ParenExpr":
        child = _render_condition_expression(children[-1]) if children else None
        return f"({child})" if child else None
    if kind in {"DeclRefExpr", "ParmVarDecl", "VarDecl", "EnumConstantDecl"}:
        return _expr_name(node)
    if kind == "MemberExpr":
        member = _member_name(node) or _expr_name(node)
        base = _render_condition_expression(children[0]) if children else None
        if member and base:
            return f"{base}{'->' if node.get('isArrow') is True else '.'}{member}"
        return member
    if kind == "CallExpr":
        callee = _expr_name(_call_callee_expr(node)) or _expr_name(node) or "call"
        args = [_render_condition_expression(child) for child in children[1:]]
        rendered_args = ", ".join(arg for arg in args if arg)
        return f"{callee}({rendered_args})"
    if kind in {"BinaryOperator", "CompoundAssignOperator"}:
        lhs, rhs = _binary_operands(node)
        left = _render_condition_expression(lhs)
        right = _render_condition_expression(rhs)
        opcode = node.get("opcode")
        if left and right and isinstance(opcode, str) and opcode:
            return f"{left} {opcode} {right}"
        return left or right or _expr_name(node)
    if kind == "UnaryOperator":
        operand = _render_condition_expression(children[0]) if children else None
        opcode = node.get("opcode")
        if operand and isinstance(opcode, str) and opcode:
            if opcode in {"post++", "post--"}:
                return f"{operand}{opcode[4:]}"
            if opcode in {"++", "--"} and node.get("isPostfix") is True:
                return f"{operand}{opcode}"
            return f"{opcode}{operand}"
        return operand or _expr_name(node)
    if kind == "ArraySubscriptExpr":
        if len(children) >= 2:
            base = _render_condition_expression(children[0])
            index = _render_condition_expression(children[1])
            if base and index:
                return f"{base}[{index}]"
        return _expr_name(node)
    if kind == "ConditionalOperator":
        if len(children) >= 3:
            condition = _render_condition_expression(children[0])
            then_expr = _render_condition_expression(children[1])
            else_expr = _render_condition_expression(children[2])
            if condition and then_expr and else_expr:
                return f"{condition} ? {then_expr} : {else_expr}"
        return _expr_name(node)
    if kind in {"IntegerLiteral", "FloatingLiteral", "StringLiteral", "CharacterLiteral"}:
        value = node.get("value")
        if isinstance(value, str) and value:
            return value
    if kind == "CXXBoolLiteralExpr":
        value = node.get("value")
        if isinstance(value, bool):
            return "true" if value else "false"
    if kind == "CXXThisExpr":
        return "this"
    if kind == "UnaryExprOrTypeTraitExpr":
        argument = _render_condition_expression(children[0]) if children else _qual_type(node)
        return f"sizeof({argument})" if argument else "sizeof(...)"
    if len(children) == 1:
        return _render_condition_expression(children[0]) or _expr_name(node)
    return _expr_name(node) or (str(kind) if isinstance(kind, str) and kind else None)


def _compact_condition_text(expression: Optional[str]) -> Optional[str]:
    if expression is None:
        return None
    text = re.sub(r"\s+", " ", expression).strip()
    if len(text) <= CONDITION_TEXT_MAX_CHARS:
        return text
    return text[: CONDITION_TEXT_MAX_CHARS - 3].rstrip() + "..."


def _condition_source_parts(condition: _ConditionAnnotation) -> Tuple[Optional[str], int]:
    if condition.source is None:
        return None, -1
    source, separator, line_text = condition.source.rpartition(":")
    if not separator:
        return condition.source, -1
    try:
        return source, int(line_text)
    except ValueError:
        return source, -1


def _node_children(node: Dict[str, JSONValue]) -> List[JSONValue]:
    children = node.get("inner", [])
    return children if isinstance(children, list) else []


def _is_error_recovery_node(node: Dict[str, JSONValue]) -> bool:
    if node.get("kind") == "RecoveryExpr":
        return True
    if node.get("containsErrors") is True:
        return True
    if node.get("isInvalidDecl") is True:
        return True
    return False


def _explicit_node_line(node: Dict[str, JSONValue]) -> Optional[int]:
    loc = node.get("loc")
    if isinstance(loc, dict):
        line = loc.get("line")
        if isinstance(line, int) and line > 0:
            return line
    range_data = node.get("range")
    if isinstance(range_data, dict):
        begin = range_data.get("begin")
        if isinstance(begin, dict):
            line = begin.get("line")
            if isinstance(line, int) and line > 0:
                return line
    return None


def _node_line(node: Dict[str, JSONValue]) -> int:
    line = _explicit_node_line(node)
    if line is not None:
        return line
    return 1


def _node_file(node: Dict[str, JSONValue]) -> Optional[str]:
    loc = node.get("loc")
    value = _location_file(loc)
    if value is not None:
        return value
    range_data = node.get("range")
    if isinstance(range_data, dict):
        begin = range_data.get("begin")
        value = _location_file(begin)
        if value is not None:
            return value
    return None


def _node_has_included_from(node: Dict[str, JSONValue]) -> bool:
    if _location_has_included_from(node.get("loc")):
        return True
    range_data = node.get("range")
    if isinstance(range_data, dict):
        begin = range_data.get("begin")
        if _location_has_included_from(begin):
            return True
    return False


def _location_file(value: JSONValue) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    file_value = value.get("file")
    if isinstance(file_value, str) and file_value:
        return file_value
    # includedFrom names the consuming translation unit for many header decls;
    # it is not a declaration source and must not drive canonical identity.
    for key in ("expansionLoc", "spellingLoc"):
        nested_file = _location_file(value.get(key))
        if nested_file is not None:
            return nested_file
    return None


def _location_has_included_from(value: JSONValue) -> bool:
    if not isinstance(value, dict):
        return False
    if "includedFrom" in value:
        return True
    for key in ("expansionLoc", "spellingLoc"):
        if _location_has_included_from(value.get(key)):
            return True
    return False


def _node_column(node: Dict[str, JSONValue]) -> Optional[int]:
    loc = node.get("loc")
    if isinstance(loc, dict):
        col = loc.get("col")
        if isinstance(col, int) and col > 0:
            return col
    range_data = node.get("range")
    if isinstance(range_data, dict):
        begin = range_data.get("begin")
        if isinstance(begin, dict):
            col = begin.get("col")
            if isinstance(col, int) and col > 0:
                return col
    return None


def _linkage_for_node(node: Optional[Dict[str, JSONValue]]) -> Optional[str]:
    if node is None:
        return None
    storage = node.get("storageClass")
    if isinstance(storage, str) and storage:
        return storage
    linkage = node.get("linkage")
    if isinstance(linkage, str) and linkage:
        return linkage
    return "unknown"


def _fact_canonical_source(fact: CodeFact) -> Optional[str]:
    canonical_source = fact.payload.get("canonical_source")
    if isinstance(canonical_source, str) and canonical_source:
        return canonical_source
    if ":" in fact.object_source:
        source, _line = fact.object_source.rsplit(":", 1)
        if source:
            return source
    return None


def _object_identity_payload(
    *,
    fact_kind: str,
    name: str,
    line_number: int,
    caller: Optional[str],
    callee: Optional[str],
    profile: str,
    payload: Dict[str, JSONValue],
) -> Dict[str, JSONValue]:
    canonical_source = payload.get("canonical_source")
    base: Dict[str, JSONValue] = {
        "version": 2,
        "kind": fact_kind,
        "name": name,
        "profile": profile,
    }
    if fact_kind == "function":
        base.update(
            {
                "canonical_source": canonical_source,
                "linkage": payload.get("linkage"),
            }
        )
    elif fact_kind == "global":
        base.update(
            {
                "canonical_source": canonical_source,
                "linkage": payload.get("linkage"),
            }
        )
    elif fact_kind == "type":
        base["canonical_source"] = canonical_source
    elif fact_kind == "field":
        base.update(
            {
                "canonical_source": canonical_source,
                "owner_name": payload.get("owner_name"),
            }
        )
    elif fact_kind == "function_pointer_slot":
        base.update(
            {
                "canonical_source": canonical_source,
                "owner_function_id": payload.get("owner_function_id"),
                "owner_function_name": payload.get("owner_function_name"),
                "line": line_number,
                "column": payload.get("column"),
            }
        )
    elif fact_kind == "code_file":
        base["canonical_source"] = name
    else:
        base.update(
            {
                "source": canonical_source,
                "line": line_number,
                "caller": caller,
                "callee": callee,
                "ordinal": payload.get("ordinal"),
            }
        )
    return base


def _is_definition(node: Dict[str, JSONValue]) -> bool:
    if node.get("isThisDeclarationADefinition") is True:
        return True
    return any(isinstance(child, dict) and child.get("kind") == "CompoundStmt" for child in _node_children(node))


def _is_top_level(node: Dict[str, JSONValue]) -> bool:
    return node.get("storageClass") != "auto" and not bool(node.get("isLocal"))


def _is_named_type(node: Dict[str, JSONValue]) -> bool:
    name = node.get("name")
    if not isinstance(name, str) or not name:
        return False
    if node.get("kind") in {"RecordDecl", "EnumDecl"} and node.get("completeDefinition") is False:
        return False
    return True


def _binary_operands(node: Dict[str, JSONValue]) -> Tuple[Optional[Dict[str, JSONValue]], Optional[Dict[str, JSONValue]]]:
    operands = [child for child in _node_children(node) if isinstance(child, dict)]
    if len(operands) < 2:
        return None, None
    return operands[0], operands[1]


def _last_child_dict(node: Dict[str, JSONValue]) -> Optional[Dict[str, JSONValue]]:
    children = [child for child in _node_children(node) if isinstance(child, dict)]
    return children[-1] if children else None


def _call_callee_expr(node: Dict[str, JSONValue]) -> Optional[Dict[str, JSONValue]]:
    children = [child for child in _node_children(node) if isinstance(child, dict)]
    return children[0] if children else None


def _unwrap_expression(node: Dict[str, JSONValue]) -> Dict[str, JSONValue]:
    current = node
    while current.get("kind") in {"ParenExpr", "ImplicitCastExpr", "CStyleCastExpr", "UnaryOperator"}:
        if current.get("kind") == "UnaryOperator" and current.get("opcode") not in {"*", "&"}:
            break
        child = _last_child_dict(current)
        if child is None:
            break
        current = child
    return current


def _qual_type(node: Dict[str, JSONValue]) -> Optional[str]:
    value = node.get("type")
    if isinstance(value, dict):
        qual_type = value.get("qualType")
        if isinstance(qual_type, str) and qual_type:
            return qual_type
    qual_type = node.get("qualType")
    if isinstance(qual_type, str) and qual_type:
        return qual_type
    return None


def _qual_type_values(node: Dict[str, JSONValue]) -> Tuple[str, ...]:
    values: List[str] = []
    value = node.get("type")
    if isinstance(value, dict):
        for key in ("qualType", "desugaredQualType"):
            text = value.get(key)
            if isinstance(text, str) and text:
                values.append(text)
    qual_type = node.get("qualType")
    if isinstance(qual_type, str) and qual_type:
        values.append(qual_type)
    return tuple(dict.fromkeys(values))


def _is_function_pointer_type(qual_type: Optional[str]) -> bool:
    if not qual_type:
        return False
    compact = re.sub(r"\s+", "", qual_type)
    return "(*)" in compact or "(*" in compact or compact.endswith("*") and "(" in compact and ")" in compact


def _node_has_function_pointer_type(node: Dict[str, JSONValue]) -> bool:
    return any(_is_function_pointer_type(qual_type) for qual_type in _qual_type_values(node))


def _node_or_referenced_decl_is_function_pointer(node: Dict[str, JSONValue]) -> bool:
    if _node_has_function_pointer_type(node):
        return True
    referenced = node.get("referencedDecl")
    if isinstance(referenced, dict) and _node_has_function_pointer_type(referenced):
        return True
    referenced_member = _referenced_field_decl(node)
    if referenced_member is not None and _node_has_function_pointer_type(referenced_member):
        return True
    return False


def _is_local_var_decl(node: Dict[str, JSONValue]) -> bool:
    if node.get("isLocal") is True:
        return True
    return node.get("storageClass") == "auto"


def _function_reference(
    target_repo: Path,
    node: Optional[Dict[str, JSONValue]],
    source_location_base: Optional[Path] = None,
    cache: Optional[_RepoRelativeSourceCache] = None,
) -> Tuple[Optional[str], Optional[str]]:
    if node is None:
        return None, None
    for current in _walk_dicts(node):
        referenced = current.get("referencedDecl")
        if isinstance(referenced, dict) and referenced.get("kind") in {"FunctionDecl", "CXXMethodDecl"}:
            return _decl_name(referenced), _canonical_source_from_decl(
                target_repo,
                referenced,
                source_location_base,
                cache,
            )
    return None, None


def _expr_name(node: Optional[Dict[str, JSONValue]]) -> Optional[str]:
    if node is None:
        return None
    for current in _walk_dicts(node):
        name = current.get("name")
        if isinstance(name, str) and name:
            return name
        referenced = current.get("referencedDecl")
        if isinstance(referenced, dict):
            ref_name = referenced.get("name")
            if isinstance(ref_name, str) and ref_name:
                return ref_name
        member = current.get("member")
        if isinstance(member, str) and member:
            return member
    return None


def _decl_name(node: Dict[str, JSONValue]) -> Optional[str]:
    name = node.get("name")
    return name if isinstance(name, str) and name else None


def _decl_owner_name(node: Dict[str, JSONValue]) -> Optional[str]:
    for key in ("ownerName", "owner_name", "parentName", "record", "record_name", "type_name"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _call_reference(
    target_repo: Path,
    node: Dict[str, JSONValue],
    source_location_base: Optional[Path] = None,
    cache: Optional[_RepoRelativeSourceCache] = None,
) -> Optional[Tuple[str, Optional[str]]]:
    callee = _call_callee_expr(node)
    if callee is None:
        return None
    for current in _walk_dicts(callee):
        referenced = current.get("referencedDecl")
        if isinstance(referenced, dict):
            kind = referenced.get("kind")
            if kind not in {"FunctionDecl", "CXXMethodDecl"}:
                return None
            name = _decl_name(referenced)
            if name:
                return name, _canonical_source_from_decl(
                    target_repo,
                    referenced,
                    source_location_base,
                    cache,
                )
    return None


def _referenced_source(
    target_repo: Path,
    node: Optional[Dict[str, JSONValue]],
    source_location_base: Optional[Path] = None,
    cache: Optional[_RepoRelativeSourceCache] = None,
) -> Optional[str]:
    if node is None:
        return None
    for current in _walk_dicts(node):
        referenced = current.get("referencedDecl")
        if isinstance(referenced, dict):
            return _canonical_source_from_decl(target_repo, referenced, source_location_base, cache)
    return None


def _canonical_source_from_decl(
    target_repo: Path,
    node: Dict[str, JSONValue],
    source_location_base: Optional[Path] = None,
    cache: Optional[_RepoRelativeSourceCache] = None,
) -> Optional[str]:
    file_value = _node_file(node)
    return _repo_relative_source_from_file_value(
        target_repo,
        source_location_base or target_repo,
        file_value,
        cache=cache,
    )


def _repo_relative_source_from_file_value(
    target_repo: Path,
    source_location_base: Path,
    file_value: Optional[str],
    *,
    cache: Optional[_RepoRelativeSourceCache] = None,
) -> Optional[str]:
    if cache is not None:
        return cache.source_from_file_value(target_repo, source_location_base, file_value)
    return _repo_relative_source_from_file_value_uncached(target_repo, source_location_base, file_value)


def _repo_relative_source_from_file_value_uncached(
    target_repo: Path,
    source_location_base: Path,
    file_value: Optional[str],
    *,
    target_resolved: Optional[Path] = None,
) -> Optional[str]:
    if not isinstance(file_value, str) or not file_value:
        return None
    candidate = Path(file_value)
    if not candidate.is_absolute():
        candidate = source_location_base / candidate
    resolved = candidate.resolve(strict=False)
    target = target_resolved if target_resolved is not None else Path(target_repo).resolve(strict=False)
    if not _is_relative_to(resolved, target) or _is_cipher_path(resolved, target):
        return None
    return resolved.relative_to(target).as_posix()


def _location_int(location: Dict[str, JSONValue], key: str) -> Optional[int]:
    value = location.get(key) if isinstance(location, dict) else None
    return value if isinstance(value, int) else None


def _header_materialization_key_from_ast_node(
    target_repo: Path,
    source_location_base: Path,
    rel_source: str,
    context_hash: str,
    node: Dict[str, JSONValue],
    cache: Optional[_RepoRelativeSourceCache] = None,
) -> Optional[str]:
    kind = node.get("kind")
    if not isinstance(kind, str) or kind not in HEADER_DECL_CACHE_KINDS:
        return None
    file_value = _node_file(node)
    canonical_source = _repo_relative_source_from_file_value(
        target_repo,
        source_location_base,
        file_value,
        cache=cache,
    )
    if canonical_source is None or canonical_source == rel_source:
        return None
    loc = node.get("loc") if isinstance(node.get("loc"), dict) else {}
    range_data = node.get("range")
    range_begin = range_data.get("begin") if isinstance(range_data, dict) and isinstance(range_data.get("begin"), dict) else {}
    identity = {
        "kind": kind,
        "usr": node.get("id") if isinstance(node.get("id"), str) else None,
        "name": node.get("name") if isinstance(node.get("name"), str) else None,
        "canonical_source": canonical_source,
        "line": _location_int(loc, "line") or _location_int(range_begin, "line"),
        "column": _location_int(loc, "col") or _location_int(range_begin, "col"),
        "range_line": _location_int(range_begin, "line"),
        "range_column": _location_int(range_begin, "col"),
        "linkage": _linkage_for_node(node),
        "tag_used": node.get("tagUsed") if isinstance(node.get("tagUsed"), str) else None,
        "context": context_hash,
    }
    return _hash_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))


def _condition_for_node(node: Dict[str, JSONValue]) -> Optional[RelativeCondition]:
    condition = node.get("cipher2_condition")
    if isinstance(condition, dict):
        return RelativeCondition.from_json(condition)
    return None


def _walk_dicts(node: Dict[str, JSONValue]) -> Iterator[Dict[str, JSONValue]]:
    yield node
    for child in _node_children(node):
        if isinstance(child, dict):
            yield from _walk_dicts(child)


def _field_access_kinds(
    node: Dict[str, JSONValue],
    parents: List[Dict[str, JSONValue]],
) -> List[Tuple[str, str]]:
    partial_read = [("field_read", "rvalue_partial")]
    unstable_operator_context = False
    for parent in reversed(parents):
        kind = parent.get("kind")
        if kind == "BinaryOperator":
            opcode = parent.get("opcode")
            lhs, _rhs = _binary_operands(parent)
            if not isinstance(opcode, str) or not opcode:
                unstable_operator_context = True
                continue
            if opcode == "=":
                if lhs is None:
                    unstable_operator_context = True
                    continue
                if _contains_dict(lhs, node):
                    return [("field_write", "assignment_lhs")]
            if opcode in COMPOUND_ASSIGN_OPS:
                if lhs is None:
                    unstable_operator_context = True
                    continue
                if _contains_dict(lhs, node):
                    return [("field_read", "read_write"), ("field_write", "read_write")]
        if kind in COMPOUND_ASSIGN_KINDS:
            lhs, _rhs = _binary_operands(parent)
            if lhs is None:
                unstable_operator_context = True
                continue
            if _contains_dict(lhs, node):
                return [("field_read", "read_write"), ("field_write", "read_write")]
        if kind == "UnaryOperator":
            opcode = parent.get("opcode")
            if opcode in INC_DEC_OPS and _contains_dict(parent, node):
                return [("field_read", "read_write"), ("field_write", "read_write")]
            if not isinstance(opcode, str) or not opcode:
                unstable_operator_context = True
    if unstable_operator_context:
        return partial_read
    if any(parent_node.get("kind") in {"IfStmt", "WhileStmt", "ForStmt", "SwitchStmt", "ConditionalOperator"} for parent_node in parents):
        return [("field_read", "condition")]
    if any(parent_node.get("kind") == "CallExpr" for parent_node in parents):
        return [("field_read", "argument")]
    if any(parent_node.get("kind") == "ReturnStmt" for parent_node in parents):
        return [("field_read", "return")]
    return [("field_read", "rvalue")]


def _field_access_wrapper_kinds(parents: List[Dict[str, JSONValue]]) -> List[str]:
    output = []
    for parent in parents:
        kind = parent.get("kind")
        if isinstance(kind, str) and kind in FIELD_ACCESS_WRAPPER_KINDS:
            output.append(kind)
    return output


def _field_access_has_bitwise_context(parents: List[Dict[str, JSONValue]]) -> bool:
    bitwise_ops = BITWISE_BINARY_OPS.union(BITWISE_COMPOUND_ASSIGN_OPS)
    return any(
        parent.get("kind") == "BinaryOperator" and parent.get("opcode") in bitwise_ops
        for parent in parents
    )


def _field_access_has_macro_expansion(node: Dict[str, JSONValue], parents: List[Dict[str, JSONValue]]) -> bool:
    return any(_node_has_macro_expansion(current) for current in [node, *parents])


def _node_has_macro_expansion(node: Dict[str, JSONValue]) -> bool:
    for key in ("loc", "range"):
        value = node.get(key)
        if isinstance(value, dict) and _location_has_macro_expansion(value):
            return True
    return False


def _location_has_macro_expansion(value: Dict[str, JSONValue]) -> bool:
    if value.get("isMacro") is True:
        return True
    if any(key in value for key in ("expansionLoc", "spellingLoc")):
        return True
    for key in ("begin", "end"):
        nested = value.get(key)
        if isinstance(nested, dict) and _location_has_macro_expansion(nested):
            return True
    return False


def _contains_dict(root: Dict[str, JSONValue], target: Dict[str, JSONValue]) -> bool:
    if root is target:
        return True
    return any(isinstance(child, dict) and _contains_dict(child, target) for child in _node_children(root))


def _member_name(node: Dict[str, JSONValue]) -> Optional[str]:
    referenced = _referenced_field_decl(node)
    if referenced is not None:
        name = _decl_name(referenced)
        if name:
            return name
    for key in ("member", "name"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    referenced = node.get("referencedDecl")
    if isinstance(referenced, dict):
        value = referenced.get("name")
        if isinstance(value, str) and value:
            return value
    return None


def _referenced_field_decl(node: Dict[str, JSONValue]) -> Optional[Dict[str, JSONValue]]:
    for key in ("referencedMemberDecl", "referencedDecl"):
        value = node.get(key)
        if isinstance(value, dict) and _decl_name(value):
            kind = value.get("kind")
            if kind is None or kind in {"FieldDecl", "IndirectFieldDecl"}:
                return value
    for current in _walk_dicts(node):
        if current is node:
            continue
        for key in ("referencedMemberDecl", "referencedDecl"):
            value = current.get(key)
            if isinstance(value, dict) and _decl_name(value):
                kind = value.get("kind")
                if kind in {"FieldDecl", "IndirectFieldDecl"}:
                    return value
    return None


def _referenced_member_decl_id(node: Dict[str, JSONValue]) -> Optional[str]:
    value = node.get("referencedMemberDecl")
    return value if isinstance(value, str) and value else None


def _member_record_name(node: Dict[str, JSONValue]) -> Optional[str]:
    referenced = _referenced_field_decl(node)
    if referenced is not None:
        owner = _decl_owner_name(referenced)
        if owner:
            return owner
    for key in ("record", "record_name", "type_name"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    for child in _node_children(node):
        if not isinstance(child, dict):
            continue
        type_data = child.get("type")
        if isinstance(type_data, dict):
            record = _record_name_from_qual_type(type_data.get("qualType"))
            if record:
                return record
    return None


def _record_name_from_qual_type(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("const ", "").replace("volatile ", "").strip()
    for prefix in ("struct ", "union ", "enum "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.replace("*", " ").replace("&", " ").strip()
    if not text:
        return None
    return text.split()[-1]

def _is_cipher_path(path: Path, target_resolved: Path) -> bool:
    try:
        rel = path.relative_to(target_resolved)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] == ".arbiter"


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000)

__all__ = [name for name in globals() if not name.startswith("__")]

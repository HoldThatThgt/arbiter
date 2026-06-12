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
from .mapper_utils import *


_LIBCLANG_ANONYMOUS_RECORD_NAME_RE = re.compile(r"^(?:struct|union|class) \(anonymous at .+\)$")


def _is_libclang_anonymous_record_name(name: str) -> bool:
    return bool(_LIBCLANG_ANONYMOUS_RECORD_NAME_RE.match(name))


class _ClangAstMapper:
    def __init__(
        self,
        target_repo: Path,
        rel_source: str,
        source_kind: str,
        profile: str,
        source_id: str,
        *,
        compile_directory: Optional[Path] = None,
        header_resolver_seed: Optional[_HeaderResolverSeed] = None,
        header_context_hash: str = "",
    ) -> None:
        self.target_repo = target_repo
        self.rel_source = rel_source
        self.source_kind = source_kind
        self.profile = profile
        self.source_id = source_id
        self.source_location_base = (
            Path(compile_directory).resolve(strict=False)
            if compile_directory is not None
            else (self.target_repo / self.rel_source).parent.resolve(strict=False)
        )
        self.facts: List[CodeFact] = []
        self.relatives: List[FactRelative] = []
        self.unresolved_calls: List[DirectCallEvidence] = []
        self.ordinal = 0
        self.fact_by_key: Dict[Tuple[str, str], CodeFact] = {}
        self.fact_by_id: Dict[str, CodeFact] = {}
        self.functions_by_name: Dict[str, List[CodeFact]] = {}
        self.types_by_name: Dict[str, List[CodeFact]] = {}
        self.field_by_identity: Dict[Tuple[str, str, str], CodeFact] = {}
        self.field_by_decl_id: Dict[str, CodeFact] = {}
        self.field_decl_by_id: Dict[str, Dict[str, JSONValue]] = {}
        self.field_decl_owner_by_id: Dict[str, str] = {}
        self.record_owner_by_node: Dict[int, _RecordOwnerIdentity] = {}
        self.field_owner_by_decl_id: Dict[str, _RecordOwnerIdentity] = {}
        self.field_decl_keys: Set[str] = set()
        self.materialized_field_decl_keys: Set[str] = set()
        self.field_facts_by_location: Dict[Tuple[str, str, int, Optional[int]], List[CodeFact]] = {}
        self.anonymous_carrier_decl_ids: Set[str] = set()
        self.fields_by_name: Dict[str, List[CodeFact]] = {}
        self.globals_by_name: Dict[str, List[CodeFact]] = {}
        self.function_pointer_slots_by_key: Dict[Tuple[str, str, str, int, Optional[int]], CodeFact] = {}
        self.assigned_slots_by_function: Dict[str, Set[str]] = {}
        self.header_context_hash = header_context_hash
        self._header_resolver_seed = header_resolver_seed or _HeaderResolverSeed()
        self._repo_relative_source_cache = _RepoRelativeSourceCache()
        self._line_by_node: Dict[int, int] = {}
        self._file_by_node: Dict[int, str] = {}
        self._compile_guard_by_line = _compile_guard_conditions_by_line(self.target_repo, self.rel_source)
        self._relative_ids: Set[str] = set()
        self._file_fact: Optional[CodeFact] = None
        self.typed_member_expr_count = 0
        self.typed_call_expr_count = 0
        self.source_from_loc_file_count = 0
        self.source_fallback_count = 0
        self.field_owner_count = 0
        self.anonymous_record_count = 0
        self.synthetic_type_fact_count = 0
        self.wrapped_member_expr_count = 0
        self.macro_wrapped_member_expr_count = 0
        self.bitwise_member_expr_count = 0
        self.compound_field_access_count = 0
        self.field_access_scan_truncated_count = 0
        self.field_access_resolved_count = 0
        self.field_access_unresolved_count = 0
        self.function_pointer_slot_count = 0
        self.function_pointer_assignment_count = 0
        self.function_pointer_dispatch_count = 0
        self.macro_direct_call_count = 0
        self.unresolved_dispatch_slot_count = 0
        self.unresolved_dispatch_function_count = 0
        self._seed_header_resolver(self._header_resolver_seed)

    def map(self, ast: Dict[str, JSONValue]) -> _FileMapResult:
        self._capture_lines(ast, 1, None)
        self._annotate_relative_conditions(ast, None)
        self._index_field_decls(ast)
        file_fact = self._add_fact("code_file", self.rel_source, 1, f"source file {self.rel_source}", payload={"path": self.rel_source})
        self._file_fact = file_fact
        for node in self._walk(ast):
            if not isinstance(node, dict):
                continue
            kind = node.get("kind")
            name = node.get("name")
            if not isinstance(name, str) or not name:
                continue
            line = self._line(node)
            if kind in {"InclusionDirective", "IncludeDirective"}:
                included = self._add_fact("code_file", name, line, f"included file {name}", payload={"path": name})
                self._add_relative(file_fact, included, "include", line)
            elif kind in {"MacroDefinitionRecord", "MacroDefinition"}:
                fact = self._add_fact("macro", name, line, f"macro {name}", payload={"name": name}, node=node)
                self._add_relative(file_fact, fact, "defines", line)
            elif kind == "FunctionDecl" and _is_definition(node):
                canonical_source = self._declaration_canonical_source(node, name)
                fact = self._add_fact(
                    "function",
                    name,
                    line,
                    f"function {name}",
                    payload={"name": name},
                    node=node,
                    canonical_source_override=canonical_source,
                    source_from_loc_override=self._source_from_node_context(node) is not None,
                )
                self._add_relative(file_fact, fact, "defines", line)
            elif kind == "VarDecl" and _is_top_level(node):
                fact = self._add_fact("global", name, line, f"global {name}", payload={"name": name}, node=node)
                self._add_relative(file_fact, fact, "defines", line)
            elif kind in {"RecordDecl", "EnumDecl", "TypedefDecl"} and _is_named_type(node):
                owner = self.record_owner_by_node.get(id(node))
                canonical_source_override = owner.canonical_source if owner is not None else None
                fact = self._add_fact(
                    "type",
                    name,
                    line,
                    f"type {name}",
                    payload={"name": name, "clang_kind": kind},
                    node=node,
                    canonical_source_override=canonical_source_override,
                    source_from_loc_override=self._source_from_node_context(node) is not None,
                )
                self._add_relative(file_fact, fact, "defines", line)

        record_nodes = [node for node in self._walk(ast) if isinstance(node, dict) and node.get("kind") in {"RecordDecl", "CXXRecordDecl"}]
        for node in reversed(record_nodes):
            self._map_fields(node)

        self._map_global_function_pointer_initializers(ast)

        for function_node in self._function_nodes(ast):
            function_name = function_node.get("name")
            if not isinstance(function_name, str):
                continue
            function_fact = self._resolve_function_fact(function_name, self._canonical_source(function_node, count=False))
            if function_fact is None:
                continue
            self._map_function_body(function_node, function_fact)
        stats = _FileMapStats(
            typed_member_expr_count=self.typed_member_expr_count,
            typed_call_expr_count=self.typed_call_expr_count,
            source_from_loc_file_count=self.source_from_loc_file_count,
            source_fallback_count=self.source_fallback_count,
            unresolved_call_count=len(self.unresolved_calls),
            field_owner_count=self.field_owner_count,
            record_owner_count=len(self.record_owner_by_node),
            anonymous_record_count=self.anonymous_record_count,
            synthetic_type_fact_count=self.synthetic_type_fact_count,
            field_decl_count=len(self.field_decl_keys),
            field_fact_count=len(self.materialized_field_decl_keys),
            field_decl_without_fact_count=len(self.field_decl_keys - self.materialized_field_decl_keys),
            wrapped_member_expr_count=self.wrapped_member_expr_count,
            macro_wrapped_member_expr_count=self.macro_wrapped_member_expr_count,
            bitwise_member_expr_count=self.bitwise_member_expr_count,
            compound_field_access_count=self.compound_field_access_count,
            field_access_scan_truncated_count=self.field_access_scan_truncated_count,
            field_access_resolved_count=self.field_access_resolved_count,
            field_access_unresolved_count=self.field_access_unresolved_count,
            function_pointer_slot_count=self.function_pointer_slot_count,
            function_pointer_assignment_count=self.function_pointer_assignment_count,
            function_pointer_dispatch_count=self.function_pointer_dispatch_count,
            macro_direct_call_count=self.macro_direct_call_count,
            unresolved_dispatch_slot_count=self.unresolved_dispatch_slot_count,
            unresolved_dispatch_function_count=self.unresolved_dispatch_function_count,
            header_decl_seed_count=self._header_resolver_seed.fact_count(),
            warning_count=(
                self.field_access_scan_truncated_count
                + self.unresolved_dispatch_slot_count
                + self.unresolved_dispatch_function_count
            ),
        )
        publishable_header_decls = tuple(self._publishable_header_decl_items(ast))
        return _FileMapResult(
            facts=self.facts,
            relatives=self.relatives,
            unresolved_calls=self.unresolved_calls,
            stats=stats,
            header_context_hash=self.header_context_hash,
            header_decl_keys=tuple(key for _node, key in publishable_header_decls),
            header_resolver_seed=self._header_seed_from_decl_nodes(tuple(node for node, _key in publishable_header_decls)),
        )

    def _seed_header_resolver(self, seed: _HeaderResolverSeed) -> None:
        for fact in seed.facts_by_id.values():
            self.fact_by_id.setdefault(fact.object_id, fact)
            self.fact_by_key.setdefault((fact.fact_kind, fact.object_name), fact)
            if fact.fact_kind == "function":
                self._append_unique_fact(self.functions_by_name, fact.object_name, fact)
            elif fact.fact_kind == "type":
                self._append_unique_fact(self.types_by_name, fact.object_name, fact)
            elif fact.fact_kind == "global":
                self._append_unique_fact(self.globals_by_name, fact.object_name, fact)
            elif fact.fact_kind == "field":
                self._append_unique_fact(self.fields_by_name, fact.object_name, fact)
        for decl_id, fact in seed.field_by_decl_id.items():
            self.field_by_decl_id.setdefault(decl_id, fact)
        for identity, fact in seed.field_by_identity.items():
            self.field_by_identity.setdefault(identity, fact)

    def _append_unique_fact(self, mapping: Dict[str, List[CodeFact]], key: str, fact: CodeFact) -> None:
        values = mapping.setdefault(key, [])
        if all(value.object_id != fact.object_id for value in values):
            values.append(fact)

    def _header_seed_from_decl_nodes(self, nodes: Sequence[Dict[str, JSONValue]]) -> _HeaderResolverSeed:
        seed = _HeaderResolverSeed()
        header_fact_ids = self._header_seed_fact_ids(nodes)
        for fact in self.facts:
            if fact.object_id in header_fact_ids:
                seed.add_fact(fact)
        for decl_id, fact in self.field_by_decl_id.items():
            if fact.object_id in header_fact_ids:
                seed.field_by_decl_id.setdefault(decl_id, fact)
        for identity, fact in self.field_by_identity.items():
            if fact.object_id in header_fact_ids:
                seed.field_by_identity.setdefault(identity, fact)
        return seed

    def _header_seed_fact_ids(self, nodes: Sequence[Dict[str, JSONValue]]) -> Set[str]:
        fact_ids: Set[str] = set()
        locations: Set[Tuple[str, int]] = set()
        for root in nodes:
            for node in self._walk(root):
                if not isinstance(node, dict):
                    continue
                canonical_source = self._canonical_source(node, count=False)
                if canonical_source != self.rel_source:
                    locations.add((canonical_source, self._line(node)))
                fact = self._header_seed_fact_for_node(node)
                if fact is not None:
                    fact_ids.add(fact.object_id)
        for fact in self.facts:
            canonical_source = fact.payload.get("canonical_source")
            line = fact.payload.get("line")
            if isinstance(canonical_source, str) and isinstance(line, int) and (canonical_source, line) in locations:
                fact_ids.add(fact.object_id)
        return fact_ids

    def _header_seed_fact_for_node(self, node: Dict[str, JSONValue]) -> Optional[CodeFact]:
        kind = node.get("kind")
        name = node.get("name")
        if kind == "FunctionDecl" and isinstance(name, str) and _is_definition(node):
            return self._resolve_function_fact(name, self._declaration_canonical_source(node, name))
        if kind == "VarDecl" and isinstance(name, str) and _is_top_level(node):
            return self._resolve_global_fact(name, self._canonical_source(node, count=False))
        if kind in {"RecordDecl", "CXXRecordDecl", "EnumDecl", "TypedefDecl"} and isinstance(name, str) and _is_named_type(node):
            owner = self.record_owner_by_node.get(id(node))
            canonical_source = owner.canonical_source if owner is not None else self._canonical_source(node, count=False)
            return self._resolve_type_fact(name, canonical_source)
        if kind in {"FieldDecl", "IndirectFieldDecl"}:
            decl_id = node.get("id")
            if isinstance(decl_id, str):
                return self.field_by_decl_id.get(decl_id)
        return None

    def _publishable_header_decl_items(self, ast: Dict[str, JSONValue]) -> Iterator[Tuple[Dict[str, JSONValue], str]]:
        if not self.header_context_hash:
            return
        has_error_cache: Dict[int, bool] = {}

        def subtree_has_error_recovery(node: Dict[str, JSONValue]) -> bool:
            node_id = id(node)
            cached = has_error_cache.get(node_id)
            if cached is not None:
                return cached
            has_error = _is_error_recovery_node(node) or any(
                subtree_has_error_recovery(child)
                for child in _node_children(node)
                if isinstance(child, dict)
            )
            has_error_cache[node_id] = has_error
            return has_error

        def visit(node: JSONValue, blocked_by_error: bool) -> Iterator[Tuple[Dict[str, JSONValue], str]]:
            if isinstance(node, dict):
                blocked = blocked_by_error or _is_error_recovery_node(node)
                if not blocked:
                    key = _header_materialization_key_from_ast_node(
                        self.target_repo,
                        self.source_location_base,
                        self.rel_source,
                        self.header_context_hash,
                        node,
                        self._repo_relative_source_cache,
                    )
                    if key is not None and not subtree_has_error_recovery(node):
                        yield node, key
                for child in _node_children(node):
                    yield from visit(child, blocked)
            elif isinstance(node, list):
                for child in node:
                    yield from visit(child, blocked_by_error)

        yield from visit(ast, False)

    def _annotate_relative_conditions(
        self,
        node: JSONValue,
        inherited_condition: Optional[_ConditionAnnotation],
    ) -> None:
        if not isinstance(node, dict) or _is_error_recovery_node(node):
            return
        active_condition = self._nearest_condition(inherited_condition, self._compile_guard_for_node(node))
        kind = node.get("kind")
        if kind in CONDITION_TARGET_KINDS and "cipher2_condition" not in node and active_condition is not None:
            node["cipher2_condition"] = active_condition.to_json()
        if kind == "IfStmt":
            self._annotate_if_statement(node, active_condition)
            return
        if kind in {"WhileStmt", "DoStmt", "ForStmt"}:
            self._annotate_loop_statement(node, active_condition)
            return
        if kind == "SwitchStmt":
            self._annotate_switch_statement(node, active_condition)
            return
        if kind in {"CaseStmt", "DefaultStmt"}:
            self._annotate_case_statement(node, active_condition)
            return
        for child in _node_children(node):
            self._annotate_relative_conditions(child, active_condition)

    def _annotate_if_statement(
        self,
        node: Dict[str, JSONValue],
        inherited_condition: Optional[_ConditionAnnotation],
    ) -> None:
        children = _dict_children(node)
        if not children:
            return
        condition_node = children[0]
        then_node = children[1] if len(children) > 1 else None
        else_node = children[2] if len(children) > 2 else None
        branch_condition = self._make_condition("branch", condition_node, branch="then")
        else_condition = self._make_condition("branch", condition_node, branch="else")
        for child in children:
            if child is condition_node:
                self._annotate_relative_conditions(child, inherited_condition)
            elif child is then_node:
                self._annotate_relative_conditions(child, branch_condition)
            elif child is else_node:
                self._annotate_relative_conditions(child, else_condition)
            else:
                self._annotate_relative_conditions(child, inherited_condition)

    def _annotate_loop_statement(
        self,
        node: Dict[str, JSONValue],
        inherited_condition: Optional[_ConditionAnnotation],
    ) -> None:
        kind = node.get("kind")
        children = _node_children(node)
        if kind == "ForStmt":
            condition_node = (
                children[2]
                if len(children) > 2 and isinstance(children[2], dict) and children[2].get("kind")
                else None
            )
            body_node = children[4] if len(children) > 4 and isinstance(children[4], dict) else None
            guard_condition = self._make_condition("loop_guard", condition_node, branch="body") if condition_node is not None else None
            for index, child in enumerate(children):
                if not isinstance(child, dict):
                    continue
                if child is body_node and guard_condition is not None:
                    self._annotate_relative_conditions(child, guard_condition)
                else:
                    self._annotate_relative_conditions(child, inherited_condition)
            return
        dict_children = _dict_children(node)
        if not dict_children:
            return
        if kind == "DoStmt":
            body_node = dict_children[0]
            condition_node = dict_children[1] if len(dict_children) > 1 else None
        else:
            condition_node = dict_children[0]
            body_node = dict_children[1] if len(dict_children) > 1 else None
        guard_condition = self._make_condition("loop_guard", condition_node, branch="body") if condition_node is not None else None
        for child in dict_children:
            if child is body_node and guard_condition is not None:
                self._annotate_relative_conditions(child, guard_condition)
            else:
                self._annotate_relative_conditions(child, inherited_condition)

    def _annotate_switch_statement(
        self,
        node: Dict[str, JSONValue],
        inherited_condition: Optional[_ConditionAnnotation],
    ) -> None:
        for child in _dict_children(node):
            self._annotate_relative_conditions(child, inherited_condition)

    def _annotate_case_statement(
        self,
        node: Dict[str, JSONValue],
        inherited_condition: Optional[_ConditionAnnotation],
    ) -> None:
        children = _dict_children(node)
        if not children:
            return
        if node.get("kind") == "DefaultStmt":
            case_condition = self._make_condition("case", node, branch="default", expression_override="default")
            body_children = children
            expression_children: List[Dict[str, JSONValue]] = []
        else:
            expression_children = children[:-1]
            body_children = children[-1:]
            expression = " ... ".join(
                item for item in (_render_condition_expression(child) for child in expression_children) if item
            )
            case_condition = self._make_condition(
                "case",
                expression_children[0] if expression_children else node,
                branch="case",
                expression_override=expression or None,
            )
        for child in expression_children:
            self._annotate_relative_conditions(child, inherited_condition)
        for child in body_children:
            self._annotate_relative_conditions(child, case_condition)

    def _make_condition(
        self,
        kind: str,
        expression_node: Optional[Dict[str, JSONValue]],
        *,
        branch: Optional[str],
        expression_override: Optional[str] = None,
    ) -> _ConditionAnnotation:
        source_node = expression_node if expression_node is not None else {}
        expression = expression_override if expression_override is not None else _render_condition_expression(expression_node)
        return _ConditionAnnotation(
            kind=kind,
            expression=_compact_condition_text(expression) if expression is not None else None,
            branch=branch,
            source=self._condition_source(source_node) if expression_node is not None else None,
        )

    def _compile_guard_for_node(self, node: Dict[str, JSONValue]) -> Optional[_ConditionAnnotation]:
        source = self._source_for_node(node)[0]
        if source != self.rel_source:
            return None
        return self._compile_guard_by_line.get(self._line(node))

    def _nearest_condition(
        self,
        inherited_condition: Optional[_ConditionAnnotation],
        compile_guard: Optional[_ConditionAnnotation],
    ) -> Optional[_ConditionAnnotation]:
        if inherited_condition is None:
            return compile_guard
        if compile_guard is None:
            return inherited_condition
        inherited_source, inherited_line = _condition_source_parts(inherited_condition)
        compile_source, compile_line = _condition_source_parts(compile_guard)
        if inherited_source == compile_source and compile_line >= inherited_line:
            return compile_guard
        return inherited_condition

    def _condition_source(self, node: Dict[str, JSONValue]) -> str:
        source = self._source_for_node(node)[0]
        return f"{source}:{self._line(node)}"

    def _index_field_decls(self, ast: Dict[str, JSONValue]) -> None:
        self._index_record_owners(ast, None)

    def _index_record_owners(self, node: Dict[str, JSONValue], parent_owner: Optional[_RecordOwnerIdentity]) -> None:
        if _is_error_recovery_node(node):
            return
        owner = parent_owner
        if node.get("kind") in {"RecordDecl", "CXXRecordDecl"}:
            owner = self._record_owner_identity(node, parent_owner)
            self.record_owner_by_node[id(node)] = owner
            if owner.owner_kind == "anonymous":
                self.anonymous_record_count += 1
            for child in _node_children(node):
                if not isinstance(child, dict):
                    continue
                if child.get("kind") in {"FieldDecl", "IndirectFieldDecl"}:
                    decl_id = child.get("id")
                    if isinstance(decl_id, str) and decl_id:
                        self.field_decl_by_id[decl_id] = child
                        self.field_decl_owner_by_id[decl_id] = owner.owner_name
                        self.field_owner_by_decl_id[decl_id] = owner
                    field_name = child.get("name")
                    if isinstance(field_name, str) and field_name:
                        self.field_decl_keys.add(self._field_decl_key(child, owner))
                    elif isinstance(decl_id, str) and decl_id:
                        self.anonymous_carrier_decl_ids.add(decl_id)
        for child in _node_children(node):
            if isinstance(child, dict):
                self._index_record_owners(child, owner)

    def _record_owner_identity(
        self,
        node: Dict[str, JSONValue],
        parent_owner: Optional[_RecordOwnerIdentity],
    ) -> _RecordOwnerIdentity:
        name = node.get("name")
        line = self._line(node)
        column = _node_column(node)
        tag_used = node.get("tagUsed")
        tag = tag_used if isinstance(tag_used, str) and tag_used else "record"
        if isinstance(name, str) and name and not _is_libclang_anonymous_record_name(name):
            owner_name = name
            owner_kind = "named"
        else:
            owner_name = "anonymous"
            owner_kind = "anonymous"
        canonical_source = self._declaration_canonical_source(node, owner_name)
        if owner_kind == "anonymous":
            col_text = str(column) if column is not None else "0"
            anonymous_name = f"<anonymous-{tag}>@{canonical_source}:{line}:{col_text}"
            owner_name = f"{parent_owner.owner_name}::{anonymous_name}" if parent_owner is not None else anonymous_name
        return _RecordOwnerIdentity(
            owner_name=owner_name,
            owner_key=f"{owner_name}:{canonical_source}:{line}:{column or 0}",
            owner_kind=owner_kind,
            tag_used=tag_used if isinstance(tag_used, str) else None,
            canonical_source=canonical_source,
            line=line,
            column=column,
            parent_owner_name=parent_owner.owner_name if parent_owner is not None else None,
        )

    def _ensure_type_fact(self, owner: _RecordOwnerIdentity, node: Dict[str, JSONValue]) -> CodeFact:
        if owner.type_fact_id is not None:
            existing = self.fact_by_id.get(owner.type_fact_id)
            if existing is not None:
                return existing
        type_fact = self._resolve_type_fact(owner.owner_name, owner.canonical_source)
        if type_fact is None:
            self.synthetic_type_fact_count += 1
            type_fact = self._add_fact(
                "type",
                owner.owner_name,
                owner.line,
                f"type {owner.owner_name}",
                payload={
                    "name": owner.owner_name,
                    "clang_kind": str(node.get("kind")) if isinstance(node.get("kind"), str) else "RecordDecl",
                    "owner_kind": owner.owner_kind,
                    "owner_key": owner.owner_key,
                    "tag_used": owner.tag_used,
                    "parent_owner_name": owner.parent_owner_name,
                    "synthetic_owner": owner.owner_kind != "named",
                },
                node=node,
                canonical_source_override=owner.canonical_source,
                source_from_loc_override=self._source_from_node_context(node) is not None,
            )
            if self._file_fact is not None:
                self._add_relative(self._file_fact, type_fact, "defines", owner.line)
        owner.type_fact_id = type_fact.object_id
        return type_fact

    def _map_fields(self, node: Dict[str, JSONValue]) -> None:
        owner = self.record_owner_by_node.get(id(node))
        if owner is None:
            return
        type_fact = self._ensure_type_fact(owner, node)
        type_name = owner.owner_name
        if node.get("cipher2HeaderCacheHit") is True:
            for field_fact in self._header_resolver_seed.fields_by_owner_source.get((type_name, owner.canonical_source), []):
                line = int(field_fact.payload.get("line")) if isinstance(field_fact.payload.get("line"), int) else self._line(node)
                self._add_relative(type_fact, field_fact, "has_field", line)
            return
        for child in _node_children(node):
            if not isinstance(child, dict) or child.get("kind") not in {"FieldDecl", "IndirectFieldDecl"}:
                continue
            field_name = child.get("name")
            if not isinstance(field_name, str) or not field_name:
                continue
            if child.get("kind") == "IndirectFieldDecl":
                field_fact = self._resolve_indirect_field_fact(child, field_name)
                if field_fact is not None:
                    self._mark_field_decl_materialized(child, owner, field_fact)
                continue
            line = self._line(child)
            field_source_node = child
            canonical_source = self._source_from_node_context(child)
            if canonical_source is None:
                field_source_node = node
                canonical_source = self._source_from_node_context(node)
            source_from_context = canonical_source is not None
            if canonical_source is None:
                canonical_source = owner.canonical_source
            field_fact = self._add_fact(
                "field",
                field_name,
                line,
                f"field {field_name} of {type_name}",
                payload={
                    "name": field_name,
                    "type": type_name,
                    "owner_name": type_name,
                    "owner_type_id": type_fact.object_id,
                    "canonical_source": canonical_source,
                    "owner_kind": owner.owner_kind,
                    "owner_key": owner.owner_key,
                },
                node=field_source_node,
                canonical_source_override=canonical_source,
                source_from_loc_override=source_from_context,
                owner_name=type_name,
            )
            self.field_owner_count += 1
            self.fields_by_name.setdefault(field_name, []).append(field_fact)
            self.field_by_identity[(type_name, field_name, field_fact.payload["canonical_source"])] = field_fact
            self.field_facts_by_location.setdefault(self._field_location_key(child, field_name), []).append(field_fact)
            self._mark_field_decl_materialized(child, owner, field_fact)
            self._add_relative(type_fact, field_fact, "has_field", line)

    def _resolve_indirect_field_fact(self, node: Dict[str, JSONValue], field_name: str) -> Optional[CodeFact]:
        candidates = self.field_facts_by_location.get(self._field_location_key(node, field_name), [])
        unique_candidates = {fact.object_id: fact for fact in candidates}
        if len(unique_candidates) == 1:
            return next(iter(unique_candidates.values()))
        return None

    def _mark_field_decl_materialized(
        self,
        node: Dict[str, JSONValue],
        owner: _RecordOwnerIdentity,
        field_fact: CodeFact,
    ) -> None:
        self.materialized_field_decl_keys.add(self._field_decl_key(node, owner))
        decl_id = node.get("id")
        if isinstance(decl_id, str) and decl_id:
            self.field_decl_by_id.setdefault(decl_id, node)
            self.field_decl_owner_by_id[decl_id] = str(field_fact.payload.get("owner_name", owner.owner_name))
            self.field_owner_by_decl_id[decl_id] = owner
            self.field_by_decl_id[decl_id] = field_fact

    def _field_decl_key(self, node: Dict[str, JSONValue], owner: _RecordOwnerIdentity) -> str:
        decl_id = node.get("id")
        if isinstance(decl_id, str) and decl_id:
            return f"id:{decl_id}"
        field_name = node.get("name")
        identity = {
            "kind": node.get("kind"),
            "owner_key": owner.owner_key,
            "name": field_name if isinstance(field_name, str) else "",
            "source": self._canonical_source(node, count=False),
            "line": self._line(node),
            "column": _node_column(node),
        }
        return json.dumps(identity, sort_keys=True, separators=(",", ":"))

    def _field_location_key(self, node: Dict[str, JSONValue], field_name: str) -> Tuple[str, str, int, Optional[int]]:
        return (field_name, self._canonical_source(node, count=False), self._line(node), _node_column(node))

    def _is_anonymous_carrier_member_expr(self, node: Dict[str, JSONValue]) -> bool:
        referenced_id = _referenced_member_decl_id(node)
        if referenced_id is not None and referenced_id in self.anonymous_carrier_decl_ids:
            return True
        referenced = _referenced_field_decl(node)
        if referenced is not None and _decl_name(referenced) is None:
            return True
        return False

    def _map_function_body(self, node: Dict[str, JSONValue], function_fact: CodeFact) -> None:
        function_name = function_fact.object_name
        assigned_slot_facts: Dict[str, CodeFact] = {}
        for child in self._walk(node):
            if not isinstance(child, dict):
                continue
            kind = child.get("kind")
            if kind == "BinaryOperator" and child.get("opcode") == "=":
                lhs, rhs = _binary_operands(child)
                endpoint = self._map_function_pointer_assignment(
                    lhs,
                    rhs,
                    child,
                    function_fact=function_fact,
                    access_context="assignment_rhs",
                )
                if endpoint is not None and endpoint.fact_kind == "function_pointer_slot":
                    assigned_slot_facts[endpoint.object_name] = endpoint
            elif kind == "VarDecl":
                endpoint = self._map_var_decl_function_pointer_initializer(child, function_fact=function_fact)
                if endpoint is not None and endpoint.fact_kind == "function_pointer_slot":
                    assigned_slot_facts[endpoint.object_name] = endpoint
            elif kind == "CallExpr":
                dispatch_handled = self._map_function_pointer_dispatch(child, function_fact)
                if dispatch_handled:
                    continue
                call_reference = _call_reference(
                    self.target_repo,
                    child,
                    self.source_location_base,
                    self._repo_relative_source_cache,
                )
                if call_reference is None:
                    continue
                callee_name, referenced_source = call_reference
                self.typed_call_expr_count += 1
                if not callee_name or callee_name in CONTROL_WORDS or callee_name == function_name:
                    continue
                line = self._line(child)
                target_fact = self._resolve_function_fact(callee_name, referenced_source)
                if target_fact is not None:
                    self._add_relative(function_fact, target_fact, "direct_call", line, condition=_condition_for_node(child))
                    if _field_access_has_macro_expansion(child, []):
                        self.macro_direct_call_count += 1
                elif callee_name in assigned_slot_facts:
                    slot_fact = assigned_slot_facts[callee_name]
                    self._add_relative(
                        function_fact,
                        slot_fact,
                        "dispatches_via",
                        line,
                        condition=_condition_for_node(child),
                        extra_payload={
                            "access_context": "indirect_call",
                            "slot_name": slot_fact.object_name,
                            "slot_kind": slot_fact.fact_kind,
                            "macro_expanded": _field_access_has_macro_expansion(child, []),
                        },
                    )
                    self.function_pointer_dispatch_count += 1
                else:
                    self.unresolved_calls.append(
                        DirectCallEvidence(
                            caller_fact_id=function_fact.object_id,
                            callee_name=callee_name,
                            referenced_source=referenced_source,
                            evidence_source=f"{self.rel_source}:{line}",
                            condition=_condition_for_node(child),
                        )
                    )
        self._map_field_accesses(node, function_fact)

    def _map_global_function_pointer_initializers(self, ast: Dict[str, JSONValue]) -> None:
        for node in self._walk(ast):
            if not isinstance(node, dict) or node.get("kind") != "VarDecl" or not _is_top_level(node):
                continue
            self._map_var_decl_function_pointer_initializer(node, function_fact=None)

    def _map_var_decl_function_pointer_initializer(
        self,
        node: Dict[str, JSONValue],
        *,
        function_fact: Optional[CodeFact],
    ) -> Optional[CodeFact]:
        children = [child for child in _node_children(node) if isinstance(child, dict)]
        if not children:
            return None
        endpoint = None
        if _node_has_function_pointer_type(node):
            rhs = self._var_decl_initializer_child(children)
            if rhs is not None:
                endpoint = self._map_function_pointer_assignment(
                    node,
                    rhs,
                    node,
                    function_fact=function_fact,
                    access_context="initializer",
                )
        for child in children:
            self._map_designated_function_pointer_initializers(child, function_fact=function_fact)
        return endpoint

    def _var_decl_initializer_child(self, children: Sequence[Dict[str, JSONValue]]) -> Optional[Dict[str, JSONValue]]:
        for child in reversed(children):
            target_name, _target_source = _function_reference(
                self.target_repo,
                child,
                self.source_location_base,
                self._repo_relative_source_cache,
            )
            if target_name:
                return child
        return None

    def _map_designated_function_pointer_initializers(
        self,
        node: Dict[str, JSONValue],
        *,
        function_fact: Optional[CodeFact],
    ) -> None:
        for current in _walk_dicts(node):
            if current.get("kind") != "DesignatedInitExpr":
                continue
            endpoint, endpoint_candidate = self._resolve_designated_field_endpoint(current)
            if endpoint is None:
                if endpoint_candidate:
                    self.unresolved_dispatch_slot_count += 1
                continue
            rhs = _last_child_dict(current)
            self._add_function_pointer_assignment(
                endpoint,
                rhs,
                current,
                access_context="initializer",
            )

    def _map_function_pointer_assignment(
        self,
        lhs: Optional[Dict[str, JSONValue]],
        rhs: Optional[Dict[str, JSONValue]],
        evidence_node: Dict[str, JSONValue],
        *,
        function_fact: Optional[CodeFact],
        access_context: str,
    ) -> Optional[CodeFact]:
        endpoint, endpoint_candidate = self._resolve_function_pointer_endpoint(
            lhs,
            function_fact=function_fact,
            count_non_pointer_candidate=False,
        )
        if endpoint is None:
            if endpoint_candidate:
                self.unresolved_dispatch_slot_count += 1
            return None
        self._add_function_pointer_assignment(endpoint, rhs, evidence_node, access_context=access_context)
        return endpoint

    def _add_function_pointer_assignment(
        self,
        endpoint: CodeFact,
        rhs: Optional[Dict[str, JSONValue]],
        evidence_node: Dict[str, JSONValue],
        *,
        access_context: str,
    ) -> None:
        target_name, target_source = _function_reference(
            self.target_repo,
            rhs,
            self.source_location_base,
            self._repo_relative_source_cache,
        )
        target_fact = self._resolve_function_fact(target_name or "", target_source)
        if target_fact is None:
            self.unresolved_dispatch_function_count += 1
            return
        line = self._line(evidence_node)
        self._add_relative(
            endpoint,
            target_fact,
            "assigned_to",
            line,
            condition=_condition_for_node(evidence_node),
            extra_payload={
                "access_context": access_context,
                "target_name": target_fact.object_name,
                "macro_expanded": _field_access_has_macro_expansion(evidence_node, []),
            },
        )
        self.function_pointer_assignment_count += 1

    def _map_function_pointer_dispatch(self, call_node: Dict[str, JSONValue], function_fact: CodeFact) -> bool:
        callee = _call_callee_expr(call_node)
        endpoint, endpoint_candidate = self._resolve_function_pointer_endpoint(
            callee,
            function_fact=function_fact,
            count_non_pointer_candidate=True,
        )
        if endpoint is None:
            if endpoint_candidate:
                self.unresolved_dispatch_slot_count += 1
                return True
            return False
        line = self._line(call_node)
        self._add_relative(
            function_fact,
            endpoint,
            "dispatches_via",
            line,
            condition=_condition_for_node(call_node),
            extra_payload={
                "access_context": "indirect_call",
                "slot_name": endpoint.object_name,
                "slot_kind": endpoint.fact_kind,
                "macro_expanded": _field_access_has_macro_expansion(call_node, []),
            },
        )
        self.function_pointer_dispatch_count += 1
        return True

    def _resolve_designated_field_endpoint(self, node: Dict[str, JSONValue]) -> Tuple[Optional[CodeFact], bool]:
        referenced = node.get("referencedDecl")
        if isinstance(referenced, dict):
            if not _node_has_function_pointer_type(referenced):
                return None, False
            referenced_id = referenced.get("id")
            if isinstance(referenced_id, str):
                fact = self.field_by_decl_id.get(referenced_id)
                if fact is not None:
                    return fact, True
            name = _decl_name(referenced)
            if name:
                candidates = self.fields_by_name.get(name, [])
                if len(candidates) == 1:
                    return candidates[0], True
            return None, True
        return None, False

    def _resolve_function_pointer_endpoint(
        self,
        node: Optional[Dict[str, JSONValue]],
        *,
        function_fact: Optional[CodeFact],
        count_non_pointer_candidate: bool,
    ) -> Tuple[Optional[CodeFact], bool]:
        if node is None:
            return None, False
        current = _unwrap_expression(node)
        kind = current.get("kind")
        if kind == "MemberExpr":
            if not _node_or_referenced_decl_is_function_pointer(current):
                return None, count_non_pointer_candidate
            return self._resolve_member_field(current), True
        if kind == "VarDecl":
            return self._resolve_var_decl_endpoint(
                current,
                function_fact=function_fact,
                count_non_pointer_candidate=count_non_pointer_candidate,
            )
        if kind == "DeclRefExpr":
            referenced = current.get("referencedDecl")
            if isinstance(referenced, dict) and referenced.get("kind") in {"VarDecl", "ParmVarDecl"}:
                return self._resolve_var_decl_endpoint(
                    referenced,
                    fallback_node=current,
                    function_fact=function_fact,
                    count_non_pointer_candidate=count_non_pointer_candidate,
                )
            if _node_has_function_pointer_type(current):
                name = current.get("name")
                if isinstance(name, str) and name:
                    if function_fact is not None:
                        line = self._line(current)
                        return self._slot_fact(name, line, owner_function=function_fact, node=current), True
                    return self._resolve_global_fact(name, None), True
        return None, False

    def _resolve_var_decl_endpoint(
        self,
        node: Dict[str, JSONValue],
        *,
        function_fact: Optional[CodeFact],
        count_non_pointer_candidate: bool,
        fallback_node: Optional[Dict[str, JSONValue]] = None,
    ) -> Tuple[Optional[CodeFact], bool]:
        if not _node_has_function_pointer_type(node):
            return None, count_non_pointer_candidate
        name = _decl_name(node) or (fallback_node.get("name") if fallback_node is not None else None)
        if not isinstance(name, str) or not name:
            return None, True
        source = _canonical_source_from_decl(
            self.target_repo,
            node,
            self.source_location_base,
            self._repo_relative_source_cache,
        )
        if _is_local_var_decl(node) or (
            function_fact is not None and node.get("ownerName") == function_fact.object_name
        ):
            line = _node_line(node)
            slot_fact = self._slot_fact(name, line, owner_function=function_fact, node=node)
            return slot_fact, True
        global_fact = self._resolve_global_fact(name, source)
        return global_fact, True

    def _map_field_accesses(self, node: Dict[str, JSONValue], function_fact: CodeFact) -> None:
        for current, parents in self._walk_field_access_nodes(node):
            if current.get("kind") != "MemberExpr":
                continue
            if self._is_anonymous_carrier_member_expr(current):
                continue
            if _referenced_field_decl(current) is not None or _referenced_member_decl_id(current) is not None:
                self.typed_member_expr_count += 1
            wrapper_kinds = _field_access_wrapper_kinds(parents)
            if wrapper_kinds:
                self.wrapped_member_expr_count += 1
            if _field_access_has_macro_expansion(current, parents):
                self.macro_wrapped_member_expr_count += 1
            if _field_access_has_bitwise_context(parents):
                self.bitwise_member_expr_count += 1
            field_fact = self._resolve_member_field(current)
            if field_fact is None:
                if _referenced_field_decl(current) is not None or _referenced_member_decl_id(current) is not None:
                    self.field_access_unresolved_count += 1
                continue
            self.field_access_resolved_count += 1
            access_kinds = _field_access_kinds(current, parents)
            if any(access_context == "read_write" for _relation_kind, access_context in access_kinds):
                self.compound_field_access_count += 1
            for relation_kind, access_context in access_kinds:
                self._add_relative(
                    function_fact,
                    field_fact,
                    relation_kind,
                    self._line(current),
                    condition=_condition_for_node(current),
                    extra_payload={
                        "access_context": access_context,
                        "field_name": field_fact.payload.get("name", field_fact.object_name),
                        "record_name": field_fact.payload.get("type"),
                        "access_confidence": "exact" if access_context != "rvalue_partial" else "partial",
                    },
                )

    def _walk_field_access_nodes(
        self,
        node: Dict[str, JSONValue],
    ) -> Iterator[Tuple[Dict[str, JSONValue], List[Dict[str, JSONValue]]]]:
        stack: List[Tuple[Dict[str, JSONValue], List[Dict[str, JSONValue]]]] = [(node, [])]
        visited = 0
        while stack:
            current, parents = stack.pop()
            if _is_error_recovery_node(current):
                continue
            if len(parents) > FIELD_ACCESS_MAX_DEPTH:
                self.field_access_scan_truncated_count += 1
                continue
            if visited >= FIELD_ACCESS_MAX_NODES_PER_FUNCTION:
                self.field_access_scan_truncated_count += 1
                break
            visited += 1
            yield current, parents
            child_parents = [*parents, current]
            for child in reversed(_node_children(current)):
                if isinstance(child, dict):
                    stack.append((child, child_parents))

    def _resolve_member_field(self, node: Dict[str, JSONValue]) -> Optional[CodeFact]:
        referenced = _referenced_field_decl(node)
        referenced_id = _referenced_member_decl_id(node)
        if referenced_id is not None:
            fact = self.field_by_decl_id.get(referenced_id)
            if fact is not None:
                return fact
            fact = self._resolve_field_decl_id_fallback(referenced_id)
            if fact is not None:
                return fact
        if referenced is None:
            return None
        member_name = _decl_name(referenced) or _member_name(node)
        if not member_name:
            return None
        record_name = _decl_owner_name(referenced) or _member_record_name(node)
        canonical_source = self._canonical_source(referenced, count=False)
        if record_name:
            fact = self.field_by_identity.get((record_name, member_name, canonical_source))
            if fact is not None:
                return fact
            fact = self.field_by_identity.get((record_name, member_name, self.rel_source))
            if fact is not None:
                return fact
        candidates = self.fields_by_name.get(member_name, [])
        same_source = [fact for fact in candidates if fact.payload.get("canonical_source") == canonical_source]
        if len(same_source) == 1:
            return same_source[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _resolve_field_decl_id_fallback(self, referenced_id: str) -> Optional[CodeFact]:
        decl_node = self.field_decl_by_id.get(referenced_id)
        if decl_node is None:
            return None
        member_name = _decl_name(decl_node)
        if not member_name:
            return None
        record_name = self.field_decl_owner_by_id.get(referenced_id) or _decl_owner_name(decl_node)
        canonical_source = self._canonical_source(decl_node, count=False)
        if record_name:
            fact = self.field_by_identity.get((record_name, member_name, canonical_source))
            if fact is not None:
                return fact
            fact = self.field_by_identity.get((record_name, member_name, self.rel_source))
            if fact is not None:
                return fact
        candidates = self.fields_by_name.get(member_name, [])
        same_source = [fact for fact in candidates if fact.payload.get("canonical_source") == canonical_source]
        if len(same_source) == 1:
            return same_source[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _function_nodes(self, ast: Dict[str, JSONValue]) -> Iterator[Dict[str, JSONValue]]:
        for node in self._walk(ast):
            if isinstance(node, dict) and node.get("kind") == "FunctionDecl" and _is_definition(node):
                yield node

    def _walk(self, node: JSONValue) -> Iterator[JSONValue]:
        if isinstance(node, dict) and _is_error_recovery_node(node):
            return
        yield node
        if isinstance(node, dict):
            for child in _node_children(node):
                yield from self._walk(child)
        elif isinstance(node, list):
            for child in node:
                yield from self._walk(child)

    def _walk_dicts_with_parents(
        self,
        node: Dict[str, JSONValue],
        parents: Optional[List[Dict[str, JSONValue]]] = None,
    ) -> Iterator[Tuple[Dict[str, JSONValue], List[Dict[str, JSONValue]]]]:
        parent_list = list(parents or [])
        if _is_error_recovery_node(node):
            return
        yield node, parent_list
        for child in _node_children(node):
            if isinstance(child, dict):
                yield from self._walk_dicts_with_parents(child, [*parent_list, node])

    def _capture_lines(
        self,
        node: JSONValue,
        inherited_line: int,
        inherited_file: Optional[str],
    ) -> Tuple[int, Optional[str]]:
        if isinstance(node, dict):
            current_line = _explicit_node_line(node) or inherited_line or 1
            self._line_by_node[id(node)] = current_line
            explicit_file = _node_file(node)
            # Clang omits loc.file on many consecutive nodes while it stays in
            # the same source buffer. Preserve that file context so header
            # declarations do not fall back to the consuming translation unit.
            if explicit_file is not None:
                current_file = explicit_file
            elif _node_has_included_from(node) and self._is_current_translation_unit_file(inherited_file):
                current_file = None
            else:
                current_file = inherited_file
            if current_file:
                self._file_by_node[id(node)] = current_file
            running_line = current_line
            running_file = current_file
            for child in _node_children(node):
                running_line, child_file = self._capture_lines(child, running_line, running_file)
                if child_file:
                    running_file = child_file
            if (
                current_file is None
                and running_file is not None
                and _node_has_included_from(node)
                and not self._is_current_translation_unit_file(running_file)
            ):
                self._file_by_node[id(node)] = running_file
            return running_line, running_file
        if isinstance(node, list):
            running_line = inherited_line or 1
            running_file = inherited_file
            for child in node:
                running_line, child_file = self._capture_lines(child, running_line, running_file)
                if child_file:
                    running_file = child_file
            return running_line, running_file
        return inherited_line or 1, inherited_file

    def _line(self, node: Dict[str, JSONValue]) -> int:
        return self._line_by_node.get(id(node), _node_line(node))

    def _slot_fact(
        self,
        slot_name: str,
        line: int,
        *,
        owner_function: Optional[CodeFact] = None,
        node: Optional[Dict[str, JSONValue]] = None,
    ) -> CodeFact:
        canonical_source = self._canonical_source(node, count=False) if node is not None else self.rel_source
        owner_id = owner_function.object_id if owner_function is not None else ""
        key = (slot_name, owner_id, canonical_source, line, _node_column(node) if node is not None else None)
        existing = self.function_pointer_slots_by_key.get(key)
        if existing is not None:
            return existing
        payload: Dict[str, JSONValue] = {"name": slot_name, "slot_kind": "local"}
        if owner_function is not None:
            payload["owner_function_id"] = owner_function.object_id
            payload["owner_function_name"] = owner_function.object_name
        column = _node_column(node) if node is not None else None
        if column is not None:
            payload["column"] = column
        fact = self._add_fact(
            "function_pointer_slot",
            slot_name,
            line,
            f"function pointer slot {slot_name}",
            payload=payload,
            node=node,
        )
        self.function_pointer_slots_by_key[key] = fact
        self.function_pointer_slot_count += 1
        return fact

    def _add_fact(
        self,
        fact_kind: str,
        name: str,
        line_number: int,
        description: str,
        *,
        caller: Optional[str] = None,
        callee: Optional[str] = None,
        payload: Optional[Dict[str, JSONValue]] = None,
        node: Optional[Dict[str, JSONValue]] = None,
        canonical_source_override: Optional[str] = None,
        source_from_loc_override: Optional[bool] = None,
        owner_name: Optional[str] = None,
    ) -> CodeFact:
        if canonical_source_override is None:
            canonical_source, source_from_loc = self._source_for_node(node)
        else:
            canonical_source = canonical_source_override
            source_from_loc = bool(source_from_loc_override)
        if node is not None:
            if source_from_loc:
                self.source_from_loc_file_count += 1
            else:
                self.source_fallback_count += 1
        self.ordinal += 1
        source = f"{canonical_source}:{line_number}"
        fact_payload: Dict[str, JSONValue] = {
            "fact_kind": fact_kind,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "canonical_source": canonical_source,
            "line": line_number,
            "ordinal": self.ordinal,
        }
        linkage = _linkage_for_node(node)
        if linkage is not None:
            fact_payload["linkage"] = linkage
        if owner_name is not None:
            fact_payload["owner_name"] = owner_name
        if payload:
            fact_payload.update(payload)
        object_id = self._object_id(fact_kind, name, line_number, caller, callee, fact_payload)
        existing = self.fact_by_id.get(object_id)
        if existing is not None:
            return existing
        fact = CodeFact(
            fact_kind=fact_kind,
            object_id=object_id,
            object_name=name,
            object_description=description,
            object_source=source,
            object_profile=self.profile,
            object_caller=caller,
            object_callee=callee,
            payload=fact_payload,
        )
        self.facts.append(fact)
        self.fact_by_id[fact.object_id] = fact
        self.fact_by_key.setdefault((fact_kind, name), fact)
        if fact_kind == "function":
            self.functions_by_name.setdefault(name, []).append(fact)
        elif fact_kind == "type":
            self.types_by_name.setdefault(name, []).append(fact)
        elif fact_kind == "global":
            self.globals_by_name.setdefault(name, []).append(fact)
        return fact

    def _add_relative(
        self,
        from_fact: CodeFact,
        to_fact: CodeFact,
        relation_kind: str,
        line_number: int,
        *,
        condition: Optional[RelativeCondition] = None,
        extra_payload: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        relation_source = self._relative_source(from_fact, to_fact)
        relation_source_kind = Path(relation_source).suffix.lower().lstrip(".")
        payload: Dict[str, JSONValue] = {"line": line_number, "source_kind": relation_source_kind}
        if extra_payload:
            payload.update(extra_payload)
        identity = json.dumps(
            {
                "from": from_fact.object_id,
                "to": to_fact.object_id,
                "kind": relation_kind,
                "condition": condition.to_json() if condition is not None else None,
                "payload": payload,
                "profile": self.profile,
                "source": relation_source,
                "line": line_number,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        relative_id = f"rel:{relation_kind}:{_hash_text(identity)[:20]}"
        if relative_id in self._relative_ids:
            return
        self._relative_ids.add(relative_id)
        self.relatives.append(
            FactRelative(
                relative_id=relative_id,
                from_fact_id=from_fact.object_id,
                to_fact_id=to_fact.object_id,
                relation_kind=relation_kind,
                condition=condition,
                object_profile=self.profile,
                evidence_source=f"{relation_source}:{line_number}",
                confidence=1.0,
                payload=payload,
            )
        )

    def _relative_source(self, from_fact: CodeFact, to_fact: CodeFact) -> str:
        from_source = _fact_canonical_source(from_fact)
        to_source = _fact_canonical_source(to_fact)
        if from_source and to_source and from_source != self.rel_source and to_source != self.rel_source:
            return from_source
        return self.rel_source

    def _object_id(
        self,
        fact_kind: str,
        name: str,
        line_number: int,
        caller: Optional[str],
        callee: Optional[str],
        payload: Dict[str, JSONValue],
    ) -> str:
        identity = json.dumps(
            _object_identity_payload(
                fact_kind=fact_kind,
                name=name,
                line_number=line_number,
                caller=caller,
                callee=callee,
                profile=self.profile,
                payload=payload,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"code:{fact_kind}:{_hash_text(identity)[:20]}"

    def _source_for_node(self, node: Optional[Dict[str, JSONValue]]) -> Tuple[str, bool]:
        if node is None:
            return self.rel_source, False
        source = self._source_from_node_context(node)
        if source is not None:
            return source, True
        return self.rel_source, False

    def _canonical_source(self, node: Optional[Dict[str, JSONValue]], *, count: bool) -> str:
        canonical_source, from_loc = self._source_for_node(node)
        if count and node is not None:
            if from_loc:
                self.source_from_loc_file_count += 1
            else:
                self.source_fallback_count += 1
        return canonical_source

    def _source_from_node_context(self, node: Dict[str, JSONValue]) -> Optional[str]:
        file_value = self._file_by_node.get(id(node)) or _node_file(node)
        return _repo_relative_source_from_file_value(
            self.target_repo,
            self.source_location_base,
            file_value,
            cache=self._repo_relative_source_cache,
        )

    def _declaration_canonical_source(self, node: Dict[str, JSONValue], name: str) -> str:
        source = self._source_from_node_context(node)
        if source is not None:
            return source
        if _node_has_included_from(node):
            return self._synthetic_header_source(node, name)
        return self.rel_source

    def _synthetic_header_source(self, node: Dict[str, JSONValue], name: str) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "anonymous"
        shape = {
            "kind": node.get("kind"),
            "name": name,
            "line": self._line(node),
            "column": _node_column(node),
            "fields": [
                {
                    "kind": child.get("kind"),
                    "name": child.get("name") if isinstance(child.get("name"), str) else "",
                    "type": _qual_type(child),
                    "line": _explicit_node_line(child),
                    "column": _node_column(child),
                }
                for child in _node_children(node)
                if isinstance(child, dict) and child.get("kind") in {"FieldDecl", "IndirectFieldDecl"}
            ],
        }
        digest = _hash_text(json.dumps(shape, sort_keys=True, separators=(",", ":")))[:12]
        return f"unknown-header/{safe_name}@{self._line(node)}_{_node_column(node) or 0}_{digest}"

    def _is_current_translation_unit_file(self, file_value: Optional[str]) -> bool:
        source = _repo_relative_source_from_file_value(
            self.target_repo,
            self.source_location_base,
            file_value,
            cache=self._repo_relative_source_cache,
        )
        return source == self.rel_source

    def _resolve_function_fact(self, name: str, canonical_source: Optional[str] = None) -> Optional[CodeFact]:
        candidates = self.functions_by_name.get(name, [])
        if canonical_source:
            same_source = [fact for fact in candidates if fact.payload.get("canonical_source") == canonical_source]
            if len(same_source) == 1:
                return same_source[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _resolve_type_fact(self, name: str, canonical_source: Optional[str] = None) -> Optional[CodeFact]:
        candidates = self.types_by_name.get(name, [])
        if canonical_source:
            same_source = [fact for fact in candidates if fact.payload.get("canonical_source") == canonical_source]
            if len(same_source) == 1:
                return same_source[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _resolve_global_fact(self, name: str, canonical_source: Optional[str] = None) -> Optional[CodeFact]:
        candidates = self.globals_by_name.get(name, [])
        if canonical_source:
            same_source = [fact for fact in candidates if fact.payload.get("canonical_source") == canonical_source]
            if len(same_source) == 1:
                return same_source[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

__all__ = [name for name in globals() if not name.startswith("__")]

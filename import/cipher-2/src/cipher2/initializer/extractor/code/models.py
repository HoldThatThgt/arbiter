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

class _FrozenSlots:
    __slots__ = ("_frozen",)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise FrozenInstanceError(f"cannot assign to field {name!r}")
        object.__setattr__(self, name, value)

    def _freeze(self) -> None:
        object.__setattr__(self, "_frozen", True)


class CodeFact(_FrozenSlots):
    __slots__ = (
        "fact_kind",
        "object_id",
        "object_name",
        "object_description",
        "object_source",
        "object_profile",
        "object_caller",
        "object_callee",
        "payload",
    )

    def __init__(
        self,
        fact_kind: str,
        object_id: str,
        object_name: str,
        object_description: str,
        object_source: str,
        object_profile: str,
        object_caller: Optional[str] = None,
        object_callee: Optional[str] = None,
        payload: object = _MISSING,
    ) -> None:
        object.__setattr__(self, "fact_kind", fact_kind)
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "object_name", object_name)
        object.__setattr__(self, "object_description", object_description)
        object.__setattr__(self, "object_source", object_source)
        object.__setattr__(self, "object_profile", object_profile)
        object.__setattr__(self, "object_caller", object_caller)
        object.__setattr__(self, "object_callee", object_callee)
        object.__setattr__(self, "payload", {} if payload is _MISSING else payload)
        self._freeze()

    def __repr__(self) -> str:
        return (
            "CodeFact("
            f"fact_kind={self.fact_kind!r}, "
            f"object_id={self.object_id!r}, "
            f"object_name={self.object_name!r}, "
            f"object_description={self.object_description!r}, "
            f"object_source={self.object_source!r}, "
            f"object_profile={self.object_profile!r}, "
            f"object_caller={self.object_caller!r}, "
            f"object_callee={self.object_callee!r}, "
            f"payload={self.payload!r})"
        )

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return False
        return (
            self.fact_kind,
            self.object_id,
            self.object_name,
            self.object_description,
            self.object_source,
            self.object_profile,
            self.object_caller,
            self.object_callee,
            self.payload,
        ) == (
            other.fact_kind,
            other.object_id,
            other.object_name,
            other.object_description,
            other.object_source,
            other.object_profile,
            other.object_caller,
            other.object_callee,
            other.payload,
        )

    __hash__ = None  # type: ignore[assignment]

    def __getstate__(self) -> Tuple[object, ...]:
        return (
            self.fact_kind,
            self.object_id,
            self.object_name,
            self.object_description,
            self.object_source,
            self.object_profile,
            self.object_caller,
            self.object_callee,
            self.payload,
        )

    def __setstate__(self, state: Tuple[object, ...]) -> None:
        for name, value in zip(self.__slots__, state):
            object.__setattr__(self, name, value)
        self._freeze()

    def to_fact_record(self) -> FactRecord:
        payload = dict(self.payload)
        payload["fact_kind"] = self.fact_kind
        return FactRecord(
            object_id=self.object_id,
            object_name=self.object_name,
            object_description=self.object_description,
            object_source=self.object_source,
            object_profile=self.object_profile,
            object_caller=self.object_caller,
            object_callee=self.object_callee,
            payload=payload,
        )

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "fact_kind": self.fact_kind,
            "object_id": self.object_id,
            "object_name": self.object_name,
            "object_description": self.object_description,
            "object_source": self.object_source,
            "object_profile": self.object_profile,
            "object_caller": self.object_caller,
            "object_callee": self.object_callee,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_json(cls, row: Dict[str, JSONValue]) -> "CodeFact":
        return cls(
            fact_kind=str(row.get("fact_kind")),
            object_id=str(row.get("object_id")),
            object_name=str(row.get("object_name")),
            object_description=str(row.get("object_description")),
            object_source=str(row.get("object_source")),
            object_profile=str(row.get("object_profile")),
            object_caller=row.get("object_caller") if isinstance(row.get("object_caller"), str) else None,
            object_callee=row.get("object_callee") if isinstance(row.get("object_callee"), str) else None,
            payload=dict(row.get("payload") or {}),
        )


@dataclass(frozen=True)
class ExtractionResult:
    facts: List[CodeFact]
    relatives: List[FactRelative]
    source_inventory: List[SourceInventoryEntry]
    unresolved_calls: List["DirectCallEvidence"]
    source_count: int
    errors: List[Exception]


@dataclass(frozen=True)
class DirectCallEvidence:
    caller_fact_id: str
    callee_name: str
    referenced_source: Optional[str]
    evidence_source: str
    condition: Optional[RelativeCondition] = None

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "caller_fact_id": self.caller_fact_id,
            "callee_name": self.callee_name,
            "referenced_source": self.referenced_source,
            "evidence_source": self.evidence_source,
            "condition": self.condition.to_json() if self.condition is not None else None,
        }

    @classmethod
    def from_json(cls, row: Dict[str, JSONValue]) -> "DirectCallEvidence":
        return cls(
            caller_fact_id=str(row.get("caller_fact_id")),
            callee_name=str(row.get("callee_name")),
            referenced_source=row.get("referenced_source") if isinstance(row.get("referenced_source"), str) else None,
            evidence_source=str(row.get("evidence_source")),
            condition=RelativeCondition.from_json(row.get("condition") if isinstance(row.get("condition"), dict) else None),
        )


@dataclass
class _HeaderMaterializationStats:
    header_decl_cache_entry_count: int = 0
    header_decl_cache_hit_count: int = 0
    header_decl_cache_miss_count: int = 0
    header_decl_skipped_subtree_count: int = 0
    header_decl_seed_count: int = 0


@dataclass
class _HeaderResolverSeed:
    facts_by_id: Dict[str, CodeFact] = field(default_factory=dict)
    functions_by_name: Dict[str, List[CodeFact]] = field(default_factory=dict)
    types_by_name: Dict[str, List[CodeFact]] = field(default_factory=dict)
    globals_by_name: Dict[str, List[CodeFact]] = field(default_factory=dict)
    fields_by_name: Dict[str, List[CodeFact]] = field(default_factory=dict)
    field_by_decl_id: Dict[str, CodeFact] = field(default_factory=dict)
    field_by_identity: Dict[Tuple[str, str, str], CodeFact] = field(default_factory=dict)
    fields_by_owner_source: Dict[Tuple[str, str], List[CodeFact]] = field(default_factory=dict)

    def add_fact(self, fact: CodeFact) -> None:
        if fact.object_id in self.facts_by_id:
            return
        self.facts_by_id[fact.object_id] = fact
        if fact.fact_kind == "function":
            self.functions_by_name.setdefault(fact.object_name, []).append(fact)
        elif fact.fact_kind == "type":
            self.types_by_name.setdefault(fact.object_name, []).append(fact)
        elif fact.fact_kind == "global":
            self.globals_by_name.setdefault(fact.object_name, []).append(fact)
        elif fact.fact_kind == "field":
            self.fields_by_name.setdefault(fact.object_name, []).append(fact)
            owner_name = fact.payload.get("owner_name")
            canonical_source = fact.payload.get("canonical_source")
            if isinstance(owner_name, str) and isinstance(canonical_source, str):
                self.field_by_identity[(owner_name, fact.object_name, canonical_source)] = fact
                self.fields_by_owner_source.setdefault((owner_name, canonical_source), []).append(fact)

    def merge(self, other: "_HeaderResolverSeed") -> None:
        for fact in other.facts_by_id.values():
            self.add_fact(fact)
        for decl_id, fact in other.field_by_decl_id.items():
            self.field_by_decl_id.setdefault(decl_id, fact)
        for identity, fact in other.field_by_identity.items():
            self.field_by_identity.setdefault(identity, fact)
        for key, facts in other.fields_by_owner_source.items():
            existing = {fact.object_id for fact in self.fields_by_owner_source.get(key, [])}
            for fact in facts:
                if fact.object_id not in existing:
                    self.fields_by_owner_source.setdefault(key, []).append(fact)
                    existing.add(fact.object_id)

    def fact_count(self) -> int:
        return len(self.facts_by_id)


@dataclass(frozen=True)
class _HeaderMaterializationContext:
    cache: "_HeaderMaterializationCache"
    source_seq: int
    rel_source: str
    context_hash: str
    stats: _HeaderMaterializationStats = field(default_factory=_HeaderMaterializationStats)
    visible_keys: Optional[FrozenSet[str]] = None


class _HeaderMaterializationCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys_by_context: Dict[str, Dict[str, int]] = {}
        self._seeds: List[Tuple[int, str, _HeaderResolverSeed]] = []

    def is_materialized(self, key: str, context: _HeaderMaterializationContext) -> bool:
        if context.visible_keys is not None:
            return key in context.visible_keys
        with self._lock:
            producer_seq = self._keys_by_context.get(context.context_hash, {}).get(key)
        return producer_seq is not None and producer_seq < context.source_seq

    def visible_seed(self, source_seq: int, context_hash: str) -> _HeaderResolverSeed:
        _keys, seed = self.visible_state(source_seq, context_hash)
        return seed

    def visible_state(self, source_seq: int, context_hash: str) -> Tuple[FrozenSet[str], _HeaderResolverSeed]:
        seed = _HeaderResolverSeed()
        with self._lock:
            keys = frozenset(
                key
                for key, producer_seq in self._keys_by_context.get(context_hash, {}).items()
                if producer_seq < source_seq
            )
            visible = [item for item in self._seeds if item[0] < source_seq and item[1] == context_hash]
        for _producer_seq, _context_hash, item_seed in visible:
            seed.merge(item_seed)
        return keys, seed

    def publish(
        self,
        *,
        producer_seq: int,
        context_hash: str,
        keys: Sequence[str],
        seed: _HeaderResolverSeed,
    ) -> None:
        if not keys and seed.fact_count() == 0:
            return
        with self._lock:
            by_key = self._keys_by_context.setdefault(context_hash, {})
            for key in keys:
                by_key.setdefault(key, producer_seq)
            if seed.fact_count() > 0:
                self._seeds.append((producer_seq, context_hash, seed))

    def entry_count(self) -> int:
        with self._lock:
            return sum(len(keys) for keys in self._keys_by_context.values())


@dataclass
class _DirectCallResolutionStats:
    pending_call_count: int = 0
    resolved_call_count: int = 0
    external_unresolved_count: int = 0
    internal_unresolved_count: int = 0
    ambiguous_call_count: int = 0
    linkage_filtered_count: int = 0
    missing_caller_count: int = 0
    duplicate_relation_count: int = 0
    resolver_worker_count: int = 0
    pending_shard_count: int = 0
    function_index_entry_count: int = 0
    resolver_duration_ms: int = 0

    def to_counts(self) -> Dict[str, int]:
        return {
            "pending_call_count": self.pending_call_count,
            "resolved_call_count": self.resolved_call_count,
            "external_unresolved_count": self.external_unresolved_count,
            "internal_unresolved_count": self.internal_unresolved_count,
            "ambiguous_call_count": self.ambiguous_call_count,
            "linkage_filtered_count": self.linkage_filtered_count,
            "missing_caller_count": self.missing_caller_count,
            "duplicate_relation_count": self.duplicate_relation_count,
            "resolver_worker_count": self.resolver_worker_count,
            "pending_shard_count": self.pending_shard_count,
            "function_index_entry_count": self.function_index_entry_count,
            "resolver_duration_ms": self.resolver_duration_ms,
        }

    def has_warning(self) -> bool:
        return self.internal_unresolved_count > 0 or self.ambiguous_call_count > 0 or self.linkage_filtered_count > 0


@dataclass(frozen=True)
class _DirectCallFunction:
    object_id: str
    object_name: str
    object_source: str
    canonical_source: Optional[str]
    linkage: Optional[str]


@dataclass(frozen=True)
class _DirectCallResolutionIndex:
    functions_by_id: Dict[str, _DirectCallFunction]
    functions_by_name: Dict[str, List[_DirectCallFunction]]
    functions_by_source_name: Dict[Tuple[str, str], List[_DirectCallFunction]]


@dataclass(frozen=True)
class _DirectCallResolutionResult:
    relatives: List[FactRelative]
    stats: _DirectCallResolutionStats


@dataclass(frozen=True)
class _MapSegmentManifest:
    facts_path: Path
    relatives_path: Path
    relatives_index_path: Path
    unresolved_calls_path: Path
    fact_count: int
    relative_count: int
    unresolved_call_count: int
    fact_kind_counts: Dict[str, int]
    relation_kind_counts: Dict[str, int]
    conditional_relative_count: int
    condition_kind_count: int
    relative_line_bytes: int
    relative_index_bytes: int
    byte_count: int
    relative_map_input_count: int = 0
    relative_map_written_count: int = 0
    relative_map_skipped_exact_count: int = 0
    relative_worker_duplicate_exact_count: int = 0
    relative_worker_duplicate_conflict_count: int = 0
    relative_worker_dedup_tracked_entry_count: int = 0
    relative_worker_dedup_saturated_count: int = 0


@dataclass(frozen=True)
class _ResolvedRelativeSegmentManifest:
    relatives_path: Path
    relatives_index_path: Path
    stats: _DirectCallResolutionStats
    relative_count: int
    relative_line_bytes: int
    relative_index_bytes: int
    byte_count: int


@dataclass(frozen=True)
class _RelativeSegmentManifest:
    relatives_path: Path
    relatives_index_path: Path
    relative_count: int
    relative_line_bytes: int
    relative_index_bytes: int


@dataclass(frozen=True)
class _RelativeIndexEntry:
    relative_id: str
    from_fact_id: str
    to_fact_id: str
    relation_kind: str
    object_profile: str
    condition_json: Optional[str]
    line_byte_count: int
    line_sha256: str


@dataclass(frozen=True)
class _IndexedRelativeLine:
    entry: _RelativeIndexEntry
    line_text: str

    def to_encoded_relative_line(self) -> EncodedRelativeLine:
        condition = json.loads(self.entry.condition_json) if self.entry.condition_json is not None else None
        return EncodedRelativeLine(
            relative_id=self.entry.relative_id,
            from_fact_id=self.entry.from_fact_id,
            to_fact_id=self.entry.to_fact_id,
            relation_kind=self.entry.relation_kind,
            condition=condition,
            object_profile=self.entry.object_profile,
            line_text=self.line_text,
        )


@dataclass
class _RelativeExternalMergeStats:
    input_count: int = 0
    accepted_count: int = 0
    duplicate_exact_count: int = 0
    conflict_count: int = 0
    segment_count: int = 0
    input_bytes: int = 0
    index_bytes: int = 0
    duration_ms: float = 0.0
    full_parse_count: int = 0
    max_heap_size: int = 0
    fan_in: int = 0
    pass_count: int = 0
    peak_open_segment_count: int = 0

    def to_counts(self) -> Dict[str, int]:
        return {
            "relative_merge_input_count": self.input_count,
            "relative_merge_accepted_count": self.accepted_count,
            "relative_merge_duplicate_exact_count": self.duplicate_exact_count,
            "relative_merge_conflict_count": self.conflict_count,
            "relative_merge_segment_count": self.segment_count,
            "relative_merge_input_bytes": self.input_bytes,
            "relative_merge_index_bytes": self.index_bytes,
            "relative_merge_duration_ms": round(self.duration_ms),
            "relative_merge_full_parse_count": self.full_parse_count,
            "relative_merge_max_heap_size": self.max_heap_size,
            "relative_merge_fan_in": self.fan_in,
            "relative_merge_pass_count": self.pass_count,
            "relative_merge_peak_open_segment_count": self.peak_open_segment_count,
        }


@dataclass
class _RecordOwnerIdentity:
    owner_name: str
    owner_key: str
    owner_kind: str
    tag_used: Optional[str]
    canonical_source: str
    line: int
    column: Optional[int]
    parent_owner_name: Optional[str]
    type_fact_id: Optional[str] = None


@dataclass(frozen=True)
class _FileMapStats:
    typed_member_expr_count: int = 0
    typed_call_expr_count: int = 0
    source_from_loc_file_count: int = 0
    source_fallback_count: int = 0
    unresolved_call_count: int = 0
    field_owner_count: int = 0
    record_owner_count: int = 0
    anonymous_record_count: int = 0
    synthetic_type_fact_count: int = 0
    field_decl_count: int = 0
    field_fact_count: int = 0
    field_decl_without_fact_count: int = 0
    wrapped_member_expr_count: int = 0
    macro_wrapped_member_expr_count: int = 0
    bitwise_member_expr_count: int = 0
    compound_field_access_count: int = 0
    field_access_scan_truncated_count: int = 0
    field_access_resolved_count: int = 0
    field_access_unresolved_count: int = 0
    function_pointer_slot_count: int = 0
    function_pointer_assignment_count: int = 0
    function_pointer_dispatch_count: int = 0
    macro_direct_call_count: int = 0
    unresolved_dispatch_slot_count: int = 0
    unresolved_dispatch_function_count: int = 0
    header_decl_cache_entry_count: int = 0
    header_decl_cache_hit_count: int = 0
    header_decl_cache_miss_count: int = 0
    header_decl_skipped_subtree_count: int = 0
    header_decl_seed_count: int = 0
    partial_ast_count: int = 0
    warning_count: int = 0


@dataclass(frozen=True)
class _FileMapResult:
    facts: List[CodeFact]
    relatives: List[FactRelative]
    unresolved_calls: List[DirectCallEvidence]
    stats: _FileMapStats
    ast_diagnostic_kind: str = "ok"
    ast_diagnostic_reason: str = "ok"
    warning_code: Optional[str] = None
    backend: str = "libclang"
    parse_duration_ms: float = 0.0
    traverse_duration_ms: float = 0.0
    header_context_hash: str = ""
    header_decl_keys: Tuple[str, ...] = ()
    header_resolver_seed: _HeaderResolverSeed = field(default_factory=_HeaderResolverSeed)


@dataclass(frozen=True)
class _FileWorkItem:
    seq: int
    source: Path
    rel_source: str
    profile: str
    source_id: str
    compile_lookup: "_CompileCommandLookup"
    segment_dir: Optional[Path] = None


@dataclass(frozen=True)
class _FileWorkOutcome:
    seq: int
    source: Path
    rel_source: str
    profile: str
    compile_lookup: "_CompileCommandLookup"
    started: float
    file_result: Optional[_FileMapResult] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    diagnostic_kind: str = "ok"
    diagnostic_reason: Optional[str] = None
    diagnostic_details: Dict[str, JSONValue] = field(default_factory=dict)
    worker_id: Optional[int] = None
    worker_header_cache_entry_count: Optional[int] = None
    segment_manifest: Optional[_MapSegmentManifest] = None


@dataclass(frozen=True)
class _AstLoadResult:
    ast: Dict[str, JSONValue]
    diagnostic_kind: str = "ok"
    diagnostic_reason: str = "ok"
    partial: bool = False
    warning_code: Optional[str] = None
    backend: str = "libclang"
    parse_duration_ms: float = 0.0


@dataclass(frozen=True)
class _ConditionAnnotation:
    kind: str
    expression: Optional[str]
    branch: Optional[str]
    source: Optional[str]

    def to_json(self) -> Dict[str, Optional[str]]:
        return {
            "kind": self.kind,
            "expression": self.expression,
            "branch": self.branch,
            "source": self.source,
        }


@dataclass(frozen=True)
class ToolchainProbeResult:
    clang_executable: str
    clang_vendor: str
    clang_version: Optional[str]
    ast_json_supported: bool
    type_driven_ast: bool
    loc_file_supported: bool
    call_reference_supported: bool
    member_reference_supported: bool
    qual_type_supported: bool
    ast_root_kind: Optional[str]
    gcc_required: bool
    gcc_checked: bool
    warning_codes: List[str] = field(default_factory=list)
    backend: str = "libclang"
    libclang_library: Optional[str] = None
    libclang_library_scope: str = "auto"
    libclang_version: Optional[str] = None
    version_match: bool = True


@dataclass(frozen=True)
class _ProcessWorkerBackendSpec:
    kind: str
    in_memory_ast: Optional[Dict[str, JSONValue]] = None
    toolchain_probe_result: Optional[ToolchainProbeResult] = None


@dataclass(frozen=True)
class _CompileCommandEntry:
    source_path: Path
    directory_path: Path
    flags: List[str]
    raw_argument_count: int
    sanitized_argument_count: int
    stripped_argument_count: int
    command_hash: str


@dataclass
class _CompileCommandStats:
    entry_count: int = 0
    indexed_source_count: int = 0
    duplicate_source_count: int = 0
    ignored_outside_repo_count: int = 0
    malformed_entry_count: int = 0
    stripped_argument_count: int = 0
    lookup_hit_count: int = 0
    lookup_miss_count: int = 0


@dataclass(frozen=True)
class _CompileCommandLookup:
    configured: bool
    matched: bool
    entry: Optional[_CompileCommandEntry]
    flags: List[str]
    command_hash: Optional[str]
    argument_count: int
    stripped_argument_count: int


@dataclass
class _CompileCommandIndex:
    target_repo: Path
    compile_database_path: Path
    by_source: Dict[Path, _CompileCommandEntry]
    stats: _CompileCommandStats

    def lookup(self, source: Path) -> _CompileCommandLookup:
        resolved = Path(source).resolve(strict=False)
        entry = self.by_source.get(resolved)
        if entry is None:
            self.stats.lookup_miss_count += 1
            return _CompileCommandLookup(
                configured=True,
                matched=False,
                entry=None,
                flags=[],
                command_hash=None,
                argument_count=0,
                stripped_argument_count=0,
            )
        self.stats.lookup_hit_count += 1
        return _CompileCommandLookup(
            configured=True,
            matched=True,
            entry=entry,
            flags=list(entry.flags),
            command_hash=entry.command_hash,
            argument_count=entry.sanitized_argument_count,
            stripped_argument_count=entry.stripped_argument_count,
        )

__all__ = [name for name in globals() if not name.startswith("__")]

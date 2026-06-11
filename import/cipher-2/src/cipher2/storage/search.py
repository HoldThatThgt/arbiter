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

from cipher2.common import JSONValue
from cipher2.tools.log import LogError, LogEvent, open_log

from .constants import *
from .models import *
from .utils import *

@dataclass(frozen=True)
class RelationSearchQuery:
    predicate: str
    anchor: str
    anchor_kind: str
    direction: str
    relation_kinds: Tuple[str, ...]
    query_kind: str = "relation"
    depth: int = 1
    target_anchor: Optional[str] = None
    target_anchor_kind: Optional[str] = None
    anchor_role: str = "anchor"
    message: Optional[str] = None
    examples: Tuple[str, ...] = ()
    file_filters: Tuple[str, ...] = ()
    name_filters: Tuple[str, ...] = ()
    terms: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationSearchAnchorCandidate:
    fact: FactRecord
    resolution_tier: int
    exact_name: bool
    role: str = "anchor"


@dataclass(frozen=True)
class RelationSearchMatchedRelation:
    relation_kind: str
    instances: int
    representative_relative_id: str


@dataclass(frozen=True)
class RelationSearchMatch:
    fact: FactRecord
    matched_relations: Tuple[RelationSearchMatchedRelation, ...]
    instances: int
    representative_relative_id: str
    hop: int = 1


@dataclass(frozen=True)
class RelationSearchPathNode:
    fact: FactRecord
    hop: int
    relation_kind: Optional[str] = None
    representative_relative_id: Optional[str] = None
    condition: Optional[RelativeCondition] = None


@dataclass(frozen=True)
class RelationSearchResult:
    query: RelationSearchQuery
    status: str
    total: int
    matches: Tuple[RelationSearchMatch, ...] = ()
    anchor: Optional[FactRecord] = None
    anchor_candidates: Tuple[RelationSearchAnchorCandidate, ...] = ()
    query_kind: str = "relation"
    complete: bool = True
    budget_exhausted: bool = False
    budget_exhausted_kind: Optional[str] = None
    total_is_exact: bool = True
    reachable: Optional[bool] = None
    path: Tuple[RelationSearchPathNode, ...] = ()
    depth_requested: Optional[int] = None
    depth_used: Optional[int] = None
    depth_max: Optional[int] = None
    message: Optional[str] = None
    examples: Tuple[str, ...] = ()
    matched_endpoint_count: Optional[int] = None
    visited_function_count: int = 0
    frontier_edge_count: int = 0
    skipped_missing_endpoint_count: int = 0


@dataclass
class _RelationKindRollup:
    relation_kind: str
    representative: FactRelative
    instances: int = 0


@dataclass
class _RelationEndpointRollup:
    fact: FactRecord
    representative: FactRelative
    hop: int = 1
    relation_kinds: Dict[str, _RelationKindRollup] = field(default_factory=dict)
    instances: int = 0
    has_unconditional: bool = False

def _fact_source_id(fact: FactRecord) -> Optional[str]:
    value = fact.payload.get("source_id")
    return value if isinstance(value, str) and value else None


def _relative_source_id(relative: FactRelative) -> Optional[str]:
    value = relative.payload.get("source_id")
    return value if isinstance(value, str) and value else None


def _fact_kind(fact: FactRecord) -> str:
    value = fact.payload.get("fact_kind")
    return value if isinstance(value, str) and value else "fact"


def _fact_kind_search_rank(fact_kind: str) -> int:
    return FACT_KIND_SEARCH_RANKS.get(fact_kind, DEFAULT_FACT_KIND_SEARCH_RANK)


def parse_relation_search_query(query: str) -> Optional[RelationSearchQuery]:
    if not isinstance(query, str):
        raise StorageError("invalid_query", "query must be a string")
    tokens = [token for token in query.split() if token]
    relation_tokens: List[Tuple[str, str]] = []
    for token in tokens:
        predicate, separator, anchor = token.partition(":")
        if separator and (predicate in RELATION_SEARCH_DEFINITIONS or predicate == "reachable"):
            relation_tokens.append((predicate, anchor))
    if not relation_tokens:
        return None
    if len(relation_tokens) > 1:
        predicates = ", ".join(predicate for predicate, _ in relation_tokens)
        raise StorageError(
            "invalid_relation_query",
            f"relation search accepts exactly one relation predicate, but the query has {len(relation_tokens)} ({predicates}). Keep one and rerun.",
        )
    predicate, anchor = relation_tokens[0]
    if not anchor:
        raise StorageError(
            "invalid_relation_query",
            "relation predicate anchor must be non-empty. Run search('<symbol or field name>') first and copy the returned result.object_id into the relation predicate.",
        )
    target_anchor: Optional[str] = None
    target_anchor_kind: Optional[str] = None
    if predicate == "reachable":
        source_anchor, separator, target_anchor = anchor.partition("->")
        if separator != "->" or not source_anchor or not target_anchor:
            raise StorageError(
                "invalid_relation_query",
                "reachable query must use reachable:<from>-><to>. Use function object_id values returned by search('<function name>'). Example: reachable:fact:function:start->fact:function:target.",
            )
        anchor = source_anchor
        anchor_kind = "function"
        target_anchor_kind = "function"
        direction = "outgoing"
        relation_kinds = ("direct_call",)
        query_kind = "relation_reachable"
        max_depth = RELATION_REACHABLE_MAX_DEPTH
        default_depth = RELATION_REACHABLE_MAX_DEPTH
        anchor_role = "start"
    else:
        anchor_kind, direction, relation_kinds = RELATION_SEARCH_DEFINITIONS[predicate]
        query_kind = "relation"
        max_depth = RELATION_CLOSURE_MAX_DEPTH
        default_depth = 1
        anchor_role = "anchor"
    file_filters: List[str] = []
    name_filters: List[str] = []
    terms: List[str] = []
    depth = default_depth
    depth_seen = False
    depth_error: Optional[str] = None
    relation_token = f"{predicate}:{anchor}" if target_anchor is None else f"{predicate}:{anchor}->{target_anchor}"
    for token in tokens:
        if token == relation_token:
            continue
        if token.startswith("file:"):
            value = token.partition(":")[2].casefold()
            if not value:
                raise StorageError("invalid_relation_query", "file filter must be non-empty")
            file_filters.append(value)
        elif token.startswith("name:") or token.startswith("caller:"):
            value = token.partition(":")[2].casefold()
            if not value:
                raise StorageError("invalid_relation_query", "caller/name filter must be non-empty")
            name_filters.append(value)
        elif token.startswith("depth:"):
            if depth_seen:
                depth_error = "depth must appear only once"
                continue
            depth_seen = True
            value = token.partition(":")[2]
            if not value.isdecimal():
                depth_error = f"depth must be an integer from 1 to {max_depth}"
            else:
                depth = int(value)
                if depth < 1 or depth > max_depth:
                    depth_error = f"depth must be in the range 1..{max_depth}"
            if predicate in {"callers", "callees"}:
                query_kind = "relation_transitive"
            elif predicate != "reachable":
                depth_error = "depth is only supported for callers, callees, and reachable relation queries"
        elif token.startswith("condition:"):
            raise StorageError(
                "invalid_relation_query",
                "condition filter is not supported in relation search. To inspect a relation's branch condition, call detail(<fact_id>) and read the `condition` field on each relative (kind / expression / branch / source).",
            )
        else:
            terms.append(token.casefold())
    message = depth_error
    examples: Tuple[str, ...] = ()
    if depth_error is not None:
        if predicate == "reachable" and target_anchor is not None:
            examples = (f"reachable:{anchor}->{target_anchor} depth:3",)
        elif predicate in {"callers", "callees"}:
            examples = (f"{predicate}:{anchor} depth:2",)
        else:
            examples = (f"{predicate}:{anchor}",)
    if predicate in {"callers", "callees"} and depth > 1:
        query_kind = "relation_transitive"
    if predicate in {"callers", "callees"} and depth_seen:
        query_kind = "relation_transitive"
    return RelationSearchQuery(
        predicate=predicate,
        anchor=anchor,
        anchor_kind=anchor_kind,
        direction=direction,
        relation_kinds=tuple(relation_kinds),
        query_kind=query_kind,
        depth=depth,
        target_anchor=target_anchor,
        target_anchor_kind=target_anchor_kind,
        anchor_role=anchor_role,
        message=message,
        examples=examples,
        file_filters=tuple(file_filters),
        name_filters=tuple(name_filters),
        terms=tuple(terms),
    )


def _relation_search_from_records(
    query: str,
    facts: List[FactRecord],
    relatives: List[FactRelative],
    limit: int,
) -> RelationSearchResult:
    spec = parse_relation_search_query(query)
    if spec is None:
        raise StorageError("invalid_relation_query", "query is not a relation search")
    if spec.message is not None:
        return _relation_search_refinement_result(spec)
    candidates = _relation_anchor_candidates(facts, spec)
    if not candidates:
        return RelationSearchResult(
            query=spec,
            status="ok",
            total=0,
            query_kind=spec.query_kind,
            depth_requested=spec.depth,
            depth_max=_relation_query_max_depth(spec),
        )
    bounded_candidates = tuple(candidates[:limit])
    if _relation_anchor_requires_refinement(candidates):
        return RelationSearchResult(
            query=spec,
            status="needs_refinement",
            total=0,
            anchor_candidates=bounded_candidates,
            query_kind=spec.query_kind,
            complete=False,
            depth_requested=spec.depth,
            depth_max=_relation_query_max_depth(spec),
        )
    anchor = candidates[0].fact
    facts_by_id = {fact.object_id: fact for fact in facts}
    if spec.query_kind == "relation_reachable":
        target_candidates = _relation_anchor_candidates(facts, _target_relation_query(spec))
        if not target_candidates:
            return RelationSearchResult(
                query=spec,
                status="ok",
                total=0,
                anchor=anchor,
                query_kind=spec.query_kind,
                reachable=False,
                depth_requested=spec.depth,
                depth_max=_relation_query_max_depth(spec),
            )
        bounded_target_candidates = tuple(target_candidates[:limit])
        if _relation_anchor_requires_refinement(target_candidates):
            return RelationSearchResult(
                query=spec,
                status="needs_refinement",
                total=0,
                anchor=anchor,
                anchor_candidates=bounded_target_candidates,
                query_kind=spec.query_kind,
                complete=False,
                depth_requested=spec.depth,
                depth_max=_relation_query_max_depth(spec),
            )
        edge_provider = _record_call_edge_provider(facts_by_id, relatives, spec.direction)
        return _relation_reachable_from_edge_provider(spec, anchor, target_candidates[0].fact, edge_provider)
    if spec.query_kind == "relation_transitive" or spec.predicate in {"callers", "callees"}:
        edge_provider = _record_call_edge_provider(facts_by_id, relatives, spec.direction)
        return _relation_transitive_from_edge_provider(spec, anchor, edge_provider, limit)
    pairs: List[Tuple[FactRecord, FactRelative]] = []
    relation_kind_set = set(spec.relation_kinds)
    for relative in relatives:
        if relative.relation_kind not in relation_kind_set:
            continue
        if spec.direction == "incoming":
            if relative.to_fact_id != anchor.object_id:
                continue
            endpoint_id = relative.from_fact_id
        else:
            if relative.from_fact_id != anchor.object_id:
                continue
            endpoint_id = relative.to_fact_id
        endpoint = facts_by_id.get(endpoint_id)
        if endpoint is not None:
            pairs.append((endpoint, relative))
    return _relation_search_from_pairs(spec, anchor, pairs, limit)


def _relation_search_refinement_result(spec: RelationSearchQuery) -> RelationSearchResult:
    return RelationSearchResult(
        query=spec,
        status="needs_refinement",
        total=0,
        query_kind=spec.query_kind,
        complete=False,
        message=spec.message,
        examples=spec.examples,
        depth_requested=spec.depth,
        depth_max=_relation_query_max_depth(spec),
    )


def _relation_query_max_depth(spec: RelationSearchQuery) -> int:
    if spec.query_kind == "relation_reachable":
        return RELATION_REACHABLE_MAX_DEPTH
    if spec.query_kind == "relation_transitive":
        return RELATION_CLOSURE_MAX_DEPTH
    return 1


def _target_relation_query(spec: RelationSearchQuery) -> RelationSearchQuery:
    return replace(
        spec,
        anchor=spec.target_anchor or "",
        anchor_kind=spec.target_anchor_kind or spec.anchor_kind,
        anchor_role="target",
    )


def _record_call_edge_provider(
    facts_by_id: Dict[str, FactRecord],
    relatives: List[FactRelative],
    direction: str,
):
    direct_relatives = [relative for relative in relatives if relative.relation_kind == "direct_call"]
    assigned_by_slot: Dict[str, List[FactRelative]] = {}
    dispatch_by_slot: Dict[str, List[FactRelative]] = {}
    for relative in relatives:
        if relative.relation_kind == "assigned_to":
            assigned_by_slot.setdefault(relative.from_fact_id, []).append(relative)
        elif relative.relation_kind == "dispatches_via":
            dispatch_by_slot.setdefault(relative.to_fact_id, []).append(relative)

    def provider(frontier_ids: Iterable[str]) -> List[Tuple[str, FactRecord, FactRelative]]:
        frontier = set(frontier_ids)
        edges: List[Tuple[str, FactRecord, FactRelative]] = []
        for relative in direct_relatives:
            if direction == "incoming":
                if relative.to_fact_id not in frontier:
                    continue
                source_id = relative.to_fact_id
                endpoint_id = relative.from_fact_id
            else:
                if relative.from_fact_id not in frontier:
                    continue
                source_id = relative.from_fact_id
                endpoint_id = relative.to_fact_id
            endpoint = facts_by_id.get(endpoint_id)
            if endpoint is not None:
                edges.append((source_id, endpoint, relative))
        if direction == "incoming":
            for assigned in relatives:
                if assigned.relation_kind != "assigned_to" or assigned.to_fact_id not in frontier:
                    continue
                for dispatch in dispatch_by_slot.get(assigned.from_fact_id, []):
                    endpoint = facts_by_id.get(dispatch.from_fact_id)
                    if endpoint is not None:
                        edges.append((assigned.to_fact_id, endpoint, dispatch))
        else:
            for dispatch in relatives:
                if dispatch.relation_kind != "dispatches_via" or dispatch.from_fact_id not in frontier:
                    continue
                for assigned in assigned_by_slot.get(dispatch.to_fact_id, []):
                    endpoint = facts_by_id.get(assigned.to_fact_id)
                    if endpoint is not None:
                        edges.append((dispatch.from_fact_id, endpoint, dispatch))
        return sorted(
            edges,
            key=lambda item: (item[0], item[1].object_name, item[1].object_source, item[2].relative_id),
        )

    return provider


def _relation_transitive_from_edge_provider(
    spec: RelationSearchQuery,
    anchor: FactRecord,
    edge_provider,
    limit: int,
) -> RelationSearchResult:
    rollups: Dict[str, _RelationEndpointRollup] = {}
    endpoint_hops: Dict[str, int] = {}
    visited: Set[str] = {anchor.object_id}
    frontier: List[str] = [anchor.object_id]
    frontier_edge_count = 0
    budget_exhausted = False
    budget_exhausted_kind: Optional[str] = None
    skipped_missing_endpoint_count = 0
    depth_used = 0
    for hop in range(1, spec.depth + 1):
        depth_used = hop
        next_frontier: List[str] = []
        for source_id, endpoint, relative in edge_provider(frontier):
            if frontier_edge_count >= RELATION_TRANSITIVE_FRONTIER_BUDGET:
                budget_exhausted = True
                budget_exhausted_kind = "frontier_edges"
                break
            frontier_edge_count += 1
            if endpoint.object_id == anchor.object_id:
                continue
            previous_hop = endpoint_hops.get(endpoint.object_id)
            if previous_hop is not None and previous_hop < hop:
                continue
            if previous_hop is None:
                endpoint_hops[endpoint.object_id] = hop
                if endpoint.object_id not in visited:
                    if len(visited) >= RELATION_TRANSITIVE_VISITED_BUDGET:
                        budget_exhausted = True
                        budget_exhausted_kind = "visited_functions"
                        break
                    visited.add(endpoint.object_id)
                    next_frontier.append(endpoint.object_id)
            if _relation_endpoint_matches(endpoint, spec):
                _add_relation_rollup(rollups, endpoint, relative, hop)
        if budget_exhausted:
            break
        frontier = next_frontier
        if not frontier:
            break
    sorted_rollups = sorted(rollups.values(), key=_relation_endpoint_sort_key)
    matches = tuple(_relation_search_match(rollup) for rollup in sorted_rollups[:limit])
    total = len(sorted_rollups)
    return RelationSearchResult(
        query=spec,
        status="too_broad" if budget_exhausted or total > limit else "ok",
        total=total,
        matches=matches,
        anchor=anchor,
        query_kind=spec.query_kind,
        complete=not budget_exhausted,
        budget_exhausted=budget_exhausted,
        budget_exhausted_kind=budget_exhausted_kind,
        total_is_exact=not budget_exhausted,
        depth_requested=spec.depth,
        depth_used=depth_used,
        depth_max=_relation_query_max_depth(spec),
        message=_relation_budget_message(budget_exhausted_kind) if budget_exhausted else None,
        matched_endpoint_count=total,
        visited_function_count=len(visited),
        frontier_edge_count=frontier_edge_count,
        skipped_missing_endpoint_count=skipped_missing_endpoint_count,
    )


def _relation_reachable_from_edge_provider(
    spec: RelationSearchQuery,
    anchor: FactRecord,
    target: FactRecord,
    edge_provider,
) -> RelationSearchResult:
    if anchor.object_id == target.object_id:
        return RelationSearchResult(
            query=spec,
            status="ok",
            total=1,
            anchor=anchor,
            query_kind=spec.query_kind,
            complete=True,
            reachable=True,
            path=(RelationSearchPathNode(fact=anchor, hop=0),),
            depth_requested=spec.depth,
            depth_used=0,
            depth_max=_relation_query_max_depth(spec),
            matched_endpoint_count=1,
            visited_function_count=1,
        )

    visited: Set[str] = {anchor.object_id}
    facts_by_id: Dict[str, FactRecord] = {anchor.object_id: anchor, target.object_id: target}
    parents: Dict[str, Tuple[str, FactRelative, int]] = {}
    frontier: List[str] = [anchor.object_id]
    frontier_edge_count = 0
    budget_exhausted = False
    budget_exhausted_kind: Optional[str] = None
    depth_used = 0
    found = False
    for hop in range(1, spec.depth + 1):
        depth_used = hop
        next_frontier: List[str] = []
        for source_id, endpoint, relative in edge_provider(frontier):
            if frontier_edge_count >= RELATION_TRANSITIVE_FRONTIER_BUDGET:
                budget_exhausted = True
                budget_exhausted_kind = "frontier_edges"
                break
            frontier_edge_count += 1
            if endpoint.object_id in visited:
                continue
            if len(visited) >= RELATION_TRANSITIVE_VISITED_BUDGET:
                budget_exhausted = True
                budget_exhausted_kind = "visited_functions"
                break
            visited.add(endpoint.object_id)
            facts_by_id[endpoint.object_id] = endpoint
            parents[endpoint.object_id] = (source_id, relative, hop)
            if endpoint.object_id == target.object_id:
                found = True
                break
            next_frontier.append(endpoint.object_id)
        if budget_exhausted or found:
            break
        frontier = next_frontier
        if not frontier:
            break
    path: Tuple[RelationSearchPathNode, ...] = ()
    if found:
        path = _build_relation_path(anchor, target, facts_by_id, parents)
    complete = found or (not budget_exhausted and not frontier)
    return RelationSearchResult(
        query=spec,
        status="too_broad" if budget_exhausted else "ok",
        total=1 if found else 0,
        anchor=anchor,
        query_kind=spec.query_kind,
        complete=complete,
        budget_exhausted=budget_exhausted,
        budget_exhausted_kind=budget_exhausted_kind,
        total_is_exact=not budget_exhausted and complete,
        reachable=found,
        path=path,
        depth_requested=spec.depth,
        depth_used=depth_used,
        depth_max=_relation_query_max_depth(spec),
        message=_relation_budget_message(budget_exhausted_kind) if budget_exhausted else None,
        matched_endpoint_count=1 if found else 0,
        visited_function_count=len(visited),
        frontier_edge_count=frontier_edge_count,
    )


def _relation_budget_message(kind: Optional[str]) -> str:
    if kind == "visited_functions":
        return "Relation search exhausted the visited function budget; reduce depth or refine the query."
    return "Relation search exhausted the frontier edge budget; reduce depth or refine the query."


def _build_relation_path(
    anchor: FactRecord,
    target: FactRecord,
    facts_by_id: Dict[str, FactRecord],
    parents: Dict[str, Tuple[str, FactRelative, int]],
) -> Tuple[RelationSearchPathNode, ...]:
    nodes: List[RelationSearchPathNode] = []
    current_id = target.object_id
    while current_id != anchor.object_id:
        parent_id, relative, hop = parents[current_id]
        fact = facts_by_id[current_id]
        nodes.append(
            RelationSearchPathNode(
                fact=fact,
                hop=hop,
                relation_kind=relative.relation_kind,
                representative_relative_id=relative.relative_id,
                condition=relative.condition,
            )
        )
        current_id = parent_id
    nodes.append(RelationSearchPathNode(fact=anchor, hop=0))
    nodes.reverse()
    return tuple(nodes)


def _relation_search_from_pairs(
    spec: RelationSearchQuery,
    anchor: FactRecord,
    pairs: List[Tuple[FactRecord, FactRelative]],
    limit: int,
) -> RelationSearchResult:
    rollups: Dict[str, _RelationEndpointRollup] = {}
    for endpoint, relative in pairs:
        if not _relation_endpoint_matches(endpoint, spec):
            continue
        _add_relation_rollup(rollups, endpoint, relative, 1)
    sorted_rollups = sorted(rollups.values(), key=_relation_endpoint_sort_key)
    matches = tuple(_relation_search_match(rollup) for rollup in sorted_rollups[:limit])
    total = len(sorted_rollups)
    message = _empty_relation_guidance_message(spec, anchor, total)
    examples = _empty_relation_guidance_examples(spec, anchor, total)
    return RelationSearchResult(
        query=spec,
        status="too_broad" if total > limit else "ok",
        total=total,
        matches=matches,
        anchor=anchor,
        query_kind=spec.query_kind,
        complete=True,
        budget_exhausted=False,
        total_is_exact=True,
        depth_requested=spec.depth,
        depth_used=1,
        depth_max=_relation_query_max_depth(spec),
        message=message,
        examples=examples,
        matched_endpoint_count=total,
    )


def _empty_relation_guidance_message(spec: RelationSearchQuery, anchor: FactRecord, total: int) -> Optional[str]:
    if total != 0 or spec.predicate != "writers":
        return None
    return (
        "No field_write edges were found for this field anchor. "
        f"If read access is an acceptable fallback, try accessors:{anchor.object_id}; "
        f"or call detail({anchor.object_id}) to inspect field_readers and field_writers together."
    )


def _empty_relation_guidance_examples(spec: RelationSearchQuery, anchor: FactRecord, total: int) -> Tuple[str, ...]:
    if total != 0 or spec.predicate != "writers":
        return ()
    return (f"accessors:{anchor.object_id}",)


def _relation_anchor_candidates(facts: List[FactRecord], spec: RelationSearchQuery) -> List[RelationSearchAnchorCandidate]:
    anchor_key = spec.anchor.casefold()
    candidates: Dict[str, RelationSearchAnchorCandidate] = {}
    for fact in facts:
        if not _relation_anchor_kind_matches(fact, spec.anchor_kind):
            continue
        if fact.object_id == spec.anchor:
            _add_anchor_candidate(candidates, fact, 0, fact.object_name.casefold() == anchor_key, spec.anchor_role)
    if candidates:
        return sorted(candidates.values(), key=_anchor_candidate_sort_key)

    candidates = {}
    for fact in facts:
        if not _relation_anchor_kind_matches(fact, spec.anchor_kind):
            continue
        if spec.anchor_kind == "field" and _field_owner_alias_matches(fact, spec.anchor):
            _add_anchor_candidate(candidates, fact, 1, True, spec.anchor_role)
    if candidates:
        return sorted(candidates.values(), key=_anchor_candidate_sort_key)

    candidates = {}
    for fact in facts:
        if not _relation_anchor_kind_matches(fact, spec.anchor_kind):
            continue
        if fact.object_name.casefold() == anchor_key:
            _add_anchor_candidate(candidates, fact, 2, True, spec.anchor_role)
    if candidates:
        return sorted(candidates.values(), key=_anchor_candidate_sort_key)

    candidates = {}
    anchor_terms = _search_terms(spec.anchor)
    for fact in facts:
        if not _relation_anchor_kind_matches(fact, spec.anchor_kind):
            continue
        if anchor_terms and _fact_search_score(fact, anchor_terms) > 0:
            _add_anchor_candidate(candidates, fact, 3, fact.object_name.casefold() == anchor_key, spec.anchor_role)
    return sorted(candidates.values(), key=_anchor_candidate_sort_key)


def _relation_anchor_requires_refinement(candidates: List[RelationSearchAnchorCandidate]) -> bool:
    if len(candidates) > 1:
        return True
    if not candidates:
        return False
    candidate = candidates[0]
    return candidate.resolution_tier >= 3


def _add_anchor_candidate(
    candidates: Dict[str, RelationSearchAnchorCandidate],
    fact: FactRecord,
    tier: int,
    exact_name: bool,
    role: str,
) -> None:
    current = candidates.get(fact.object_id)
    candidate = RelationSearchAnchorCandidate(fact=fact, resolution_tier=tier, exact_name=exact_name, role=role)
    if current is None or _anchor_candidate_sort_key(candidate) < _anchor_candidate_sort_key(current):
        candidates[fact.object_id] = candidate


def _relation_anchor_kind_matches(fact: FactRecord, anchor_kind: str) -> bool:
    return _fact_kind(fact) == anchor_kind


def _field_owner_alias_matches(fact: FactRecord, anchor: str) -> bool:
    for owner_key, field_key in _owner_field_query_pairs(anchor):
        if fact.object_name.casefold() != field_key:
            continue
        owner_text = _field_owner_search_text(fact)
        if owner_key and owner_key in owner_text:
            return True
    return False


def _anchor_candidate_sort_key(candidate: RelationSearchAnchorCandidate) -> Tuple[int, int, str, str, str]:
    return (
        candidate.resolution_tier,
        0 if candidate.exact_name else 1,
        _endpoint_source_file(candidate.fact.object_source),
        candidate.fact.object_source,
        candidate.fact.object_id,
    )


def _relation_endpoint_matches(fact: FactRecord, spec: RelationSearchQuery) -> bool:
    endpoint_source = _endpoint_source_file(fact.object_source).casefold()
    if any(file_filter not in endpoint_source for file_filter in spec.file_filters):
        return False
    endpoint_name = fact.object_name.casefold()
    if any(name_filter not in endpoint_name for name_filter in spec.name_filters):
        return False
    if spec.terms and _fact_search_score(fact, list(spec.terms)) <= 0:
        return False
    return True


def _add_relation_rollup(
    rollups: Dict[str, _RelationEndpointRollup],
    endpoint: FactRecord,
    relative: FactRelative,
    hop: int,
) -> None:
    rollup = rollups.get(endpoint.object_id)
    if rollup is None:
        rollup = _RelationEndpointRollup(fact=endpoint, representative=relative, hop=hop)
        rollups[endpoint.object_id] = rollup
    rollup.instances += 1
    if hop < rollup.hop:
        rollup.hop = hop
    if relative.condition is None:
        rollup.has_unconditional = True
    if _relation_representative_key(relative) < _relation_representative_key(rollup.representative):
        rollup.representative = relative
    kind_rollup = rollup.relation_kinds.get(relative.relation_kind)
    if kind_rollup is None:
        kind_rollup = _RelationKindRollup(relation_kind=relative.relation_kind, representative=relative)
        rollup.relation_kinds[relative.relation_kind] = kind_rollup
    kind_rollup.instances += 1
    if _relation_representative_key(relative) < _relation_representative_key(kind_rollup.representative):
        kind_rollup.representative = relative


def _relation_endpoint_sort_key(rollup: _RelationEndpointRollup) -> Tuple[int, int, int, int, str, str, str, str]:
    return (
        rollup.hop,
        min(RELATION_SEARCH_SALIENCE_RANKS.get(kind, 5) for kind in rollup.relation_kinds),
        -rollup.instances,
        0 if rollup.has_unconditional else 1,
        rollup.fact.object_name,
        _endpoint_source_file(rollup.fact.object_source),
        rollup.fact.object_id,
        rollup.representative.relative_id,
    )


def _relation_search_match(rollup: _RelationEndpointRollup) -> RelationSearchMatch:
    matched = tuple(
        RelationSearchMatchedRelation(
            relation_kind=kind_rollup.relation_kind,
            instances=kind_rollup.instances,
            representative_relative_id=kind_rollup.representative.relative_id,
        )
        for kind_rollup in sorted(rollup.relation_kinds.values(), key=_relation_kind_rollup_sort_key)
    )
    return RelationSearchMatch(
        fact=rollup.fact,
        matched_relations=matched,
        instances=rollup.instances,
        representative_relative_id=rollup.representative.relative_id,
        hop=rollup.hop,
    )


def _relation_kind_rollup_sort_key(rollup: _RelationKindRollup) -> Tuple[int, str, str]:
    return (
        RELATION_SEARCH_SALIENCE_RANKS.get(rollup.relation_kind, 5),
        rollup.relation_kind,
        rollup.representative.relative_id,
    )


def _relation_representative_key(relative: FactRelative) -> Tuple[int, str, str]:
    return (1 if relative.condition is not None else 0, relative.evidence_source, relative.relative_id)


def _search_facts(facts: Iterable[FactRecord], query: str, limit: int) -> List[FactRecord]:
    terms = _search_terms(query)
    if not terms:
        return sorted(facts, key=lambda fact: fact.object_id)[:limit]

    matched = [(_fact_search_score(fact, terms), fact) for fact in facts]
    matched = [(item_score, fact) for item_score, fact in matched if item_score > 0]
    matched.sort(key=lambda item: _fact_search_sort_key(item[1], item[0], terms))
    return _select_ranked_search_results(matched, terms, limit)


def _select_search_results(facts: Iterable[FactRecord], query: str, limit: int) -> List[FactRecord]:
    terms = _search_terms(query)
    if not terms:
        return sorted(_dedupe_facts(facts), key=lambda fact: fact.object_id)[:limit]
    matched = [(_fact_search_score(fact, terms), fact) for fact in _dedupe_facts(facts)]
    matched = [(item_score, fact) for item_score, fact in matched if item_score > 0]
    matched.sort(key=lambda item: _fact_search_sort_key(item[1], item[0], terms))
    return _select_ranked_search_results(matched, terms, limit)


def _select_ranked_search_results(
    ranked: List[Tuple[int, FactRecord]],
    terms: List[str],
    limit: int,
) -> List[FactRecord]:
    exact_query = " ".join(terms)
    selected: List[FactRecord] = []
    selected_ids: Set[str] = set()
    exact_by_kind: Dict[str, List[Tuple[int, FactRecord]]] = {}
    for item in ranked:
        fact = item[1]
        if fact.object_name.casefold() == exact_query:
            exact_by_kind.setdefault(_fact_kind(fact), []).append(item)
    for _kind, items in sorted(
        exact_by_kind.items(),
        key=lambda pair: (-_fact_kind_search_rank(pair[0]), pair[0]),
    ):
        for _score, fact in items[:SEARCH_EXACT_KIND_FLOOR]:
            _append_selected_fact(selected, selected_ids, fact, limit)
            if len(selected) >= limit:
                return selected
    for _score, fact in ranked:
        _append_selected_fact(selected, selected_ids, fact, limit)
        if len(selected) >= limit:
            break
    return selected


def _append_selected_fact(selected: List[FactRecord], selected_ids: Set[str], fact: FactRecord, limit: int) -> None:
    if len(selected) >= limit or fact.object_id in selected_ids:
        return
    selected.append(fact)
    selected_ids.add(fact.object_id)


def _dedupe_facts(facts: Iterable[FactRecord]) -> List[FactRecord]:
    observed: Set[str] = set()
    result: List[FactRecord] = []
    for fact in facts:
        if fact.object_id in observed:
            continue
        observed.add(fact.object_id)
        result.append(fact)
    return result


def _search_candidate_limit(limit: int) -> int:
    return max(limit, min(max(limit * SEARCH_CANDIDATE_MULTIPLIER, SEARCH_CANDIDATE_MIN), 1000))


def _fact_search_sort_key(fact: FactRecord, score: int, terms: List[str]) -> Tuple[int, int, int, int, str]:
    exact_name = 1 if fact.object_name.casefold() == " ".join(terms) else 0
    kind_rank = _fact_kind_search_rank(_fact_kind(fact))
    rank_score = score + exact_name * EXACT_NAME_SEARCH_BONUS + kind_rank
    return (-rank_score, -score, -exact_name, -kind_rank, fact.object_id)


def _search_terms(query: str) -> List[str]:
    return [term.casefold() for term in query.split() if term]


def _fact_search_score(fact: FactRecord, terms: List[str]) -> int:
    fields = (
        (fact.object_name.casefold(), 3),
        (fact.object_description.casefold(), 2),
        ((fact.object_caller or "").casefold(), 2),
        ((fact.object_callee or "").casefold(), 2),
        (fact.object_source.casefold(), 1),
        (_field_owner_search_text(fact), FIELD_OWNER_SEARCH_WEIGHT),
    )
    total = 0
    for term in terms:
        term_score = sum(weight for value, weight in fields if term in value)
        if term_score == 0:
            return 0
        total += term_score
    return total


def _field_owner_search_text(fact: FactRecord) -> str:
    if _fact_kind(fact) != "field":
        return ""
    field_name = fact.object_name
    owners = []
    for payload_key in ("owner_name", "type"):
        value = fact.payload.get(payload_key)
        if isinstance(value, str) and value:
            owners.append(value)
    aliases: List[str] = []
    seen: Set[str] = set()
    for owner in owners:
        key = owner.casefold()
        if key in seen:
            continue
        seen.add(key)
        aliases.extend((owner, f"{owner}.{field_name}", f"{owner}::{field_name}"))
    return " ".join(aliases).casefold()


def _owner_field_query_pairs(query: str) -> List[Tuple[str, str]]:
    identifiers = _FIELD_QUERY_IDENTIFIER_RE.findall(query)
    if len(identifiers) < 2:
        return []
    field_name = identifiers[-1].casefold()
    owner_candidates = identifiers[:-1]
    pairs: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for owner in owner_candidates:
        _append_owner_field_query_pair(pairs, seen, owner.casefold(), field_name)
    if len(owner_candidates) > 1:
        _append_owner_field_query_pair(pairs, seen, " ".join(owner_candidates).casefold(), field_name)
    if len(owner_candidates) > 2 and owner_candidates[0].casefold() in {"struct", "union", "enum"}:
        _append_owner_field_query_pair(pairs, seen, " ".join(owner_candidates[1:]).casefold(), field_name)
    return pairs


def _append_owner_field_query_pair(
    pairs: List[Tuple[str, str]],
    seen: Set[Tuple[str, str]],
    owner_key: str,
    field_key: str,
) -> None:
    if not owner_key or not field_key:
        return
    pair = (owner_key, field_key)
    if pair in seen:
        return
    seen.add(pair)
    pairs.append(pair)


def _filter_relatives(
    relatives: Iterable[FactRelative],
    fact_id: str,
    direction: str,
    relation_kind: Optional[str],
    limit: int,
) -> List[FactRelative]:
    _validate_relative_query(fact_id, direction, relation_kind)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
        raise StorageError("invalid_limit", "limit must be between 1 and 100")
    output = [relative for relative in relatives if _relative_matches(relative, fact_id, direction, relation_kind)]
    output.sort(key=lambda item: (item.relation_kind, item.from_fact_id, item.to_fact_id, item.relative_id))
    return output[:limit]


def _validate_relative_query(fact_id: str, direction: str, relation_kind: Optional[str]) -> None:
    if not isinstance(fact_id, str) or not fact_id:
        raise StorageError("invalid_fact_id", "fact_id must be a non-empty string")
    if direction not in {"incoming", "outgoing", "both"}:
        raise StorageError("invalid_direction", "direction must be incoming, outgoing, or both")
    if relation_kind is not None and relation_kind not in RELATION_KINDS:
        raise StorageError("invalid_relation_kind", unsupported_relation_kind_message(relation_kind))


def _relative_matches(
    relative: FactRelative,
    fact_id: str,
    direction: str,
    relation_kind: Optional[str],
) -> bool:
    if relation_kind is not None and relative.relation_kind != relation_kind:
        return False
    if direction == "incoming":
        return relative.to_fact_id == fact_id
    if direction == "outgoing":
        return relative.from_fact_id == fact_id
    return relative.from_fact_id == fact_id or relative.to_fact_id == fact_id


def _relative_sql_where(
    fact_k: int,
    direction: str,
    relation_kind: Optional[str],
) -> Tuple[List[str], List[Any]]:
    if direction not in {"incoming", "outgoing", "both"}:
        raise StorageError("invalid_direction", "direction must be incoming, outgoing, or both")
    where: List[str] = []
    params: List[Any] = []
    if direction == "incoming":
        where.append("r.to_k = ?")
        params.append(fact_k)
    elif direction == "outgoing":
        where.append("r.from_k = ?")
        params.append(fact_k)
    else:
        where.append("(r.from_k = ? OR r.to_k = ?)")
        params.extend([fact_k, fact_k])
    if relation_kind is not None:
        if relation_kind not in RELATION_KINDS:
            raise StorageError("invalid_relation_kind", unsupported_relation_kind_message(relation_kind))
        where.append("r.relation_kind_code = ?")
        params.append(RELATION_KIND_CODES[relation_kind])
    return where, params

__all__ = [name for name in globals() if not name.startswith("__")]

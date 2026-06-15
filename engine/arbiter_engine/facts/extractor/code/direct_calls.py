from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Set, Tuple

from arbiter_engine.facts.store._common import JSONValue
from arbiter_engine.facts.store import FactRelative

from .mapper_utils import _hash_text
from .models import (
    CodeFact,
    DirectCallEvidence,
    _DirectCallFunction,
    _DirectCallResolutionIndex,
    _DirectCallResolutionResult,
    _DirectCallResolutionStats,
)


def _direct_call_evidence_to_json(evidence: DirectCallEvidence) -> Dict[str, JSONValue]:
    return evidence.to_json()


def _direct_call_evidence_from_json(row: Dict[str, JSONValue]) -> DirectCallEvidence:
    return DirectCallEvidence.from_json(row)


def _shard_direct_call_evidence(
    unresolved_calls: Sequence[DirectCallEvidence],
    worker_count: int,
) -> List[List[DirectCallEvidence]]:
    shard_count = max(1, min(worker_count, len(unresolved_calls)))
    shards: List[List[DirectCallEvidence]] = [[] for _ in range(shard_count)]
    for index, evidence in enumerate(unresolved_calls):
        shards[index % shard_count].append(evidence)
    return [shard for shard in shards if shard]


def _merge_direct_call_stats(target: _DirectCallResolutionStats, source: _DirectCallResolutionStats) -> None:
    target.external_unresolved_count += source.external_unresolved_count
    target.internal_unresolved_count += source.internal_unresolved_count
    target.ambiguous_call_count += source.ambiguous_call_count
    target.linkage_filtered_count += source.linkage_filtered_count
    target.missing_caller_count += source.missing_caller_count


def _passthrough_ratio_percent(passthrough_count: int, reencoded_count: int) -> int:
    total = passthrough_count + reencoded_count
    if total <= 0:
        return 100
    return round(passthrough_count * 100 / total)


def _resolve_pending_direct_call_shard(
    index: _DirectCallResolutionIndex,
    unresolved_calls: Sequence[DirectCallEvidence],
    profile: str,
) -> _DirectCallResolutionResult:
    stats = _DirectCallResolutionStats(pending_call_count=len(unresolved_calls))
    relatives: List[FactRelative] = []
    for evidence in unresolved_calls:
        caller = index.functions_by_id.get(evidence.caller_fact_id)
        if caller is None:
            stats.missing_caller_count += 1
            continue
        resolved = _select_direct_call_target(index, evidence, caller, stats)
        if resolved is None:
            continue
        target, strategy = resolved
        relatives.append(_make_resolved_direct_call_relative(caller, target, evidence, strategy, profile))
    return _DirectCallResolutionResult(relatives=relatives, stats=stats)


def _resolve_pending_direct_calls(
    facts: Sequence[CodeFact],
    unresolved_calls: Sequence[DirectCallEvidence],
    existing_relative_ids: Set[str],
    profile: str,
) -> _DirectCallResolutionResult:
    stats = _DirectCallResolutionStats(pending_call_count=len(unresolved_calls))
    if not unresolved_calls:
        return _DirectCallResolutionResult(relatives=[], stats=stats)

    index = _build_direct_call_resolution_index(facts)
    relatives: List[FactRelative] = []
    for evidence in unresolved_calls:
        caller = index.functions_by_id.get(evidence.caller_fact_id)
        if caller is None:
            stats.missing_caller_count += 1
            continue
        resolved = _select_direct_call_target(index, evidence, caller, stats)
        if resolved is None:
            continue
        target, strategy = resolved
        relative = _make_resolved_direct_call_relative(caller, target, evidence, strategy, profile)
        if relative.relative_id in existing_relative_ids:
            stats.duplicate_relation_count += 1
            continue
        existing_relative_ids.add(relative.relative_id)
        relatives.append(relative)
        stats.resolved_call_count += 1
    return _DirectCallResolutionResult(relatives=relatives, stats=stats)


def _build_direct_call_resolution_index(facts: Sequence[CodeFact]) -> _DirectCallResolutionIndex:
    functions_by_id: Dict[str, _DirectCallFunction] = {}
    functions_by_name: Dict[str, List[_DirectCallFunction]] = {}
    functions_by_source_name: Dict[Tuple[str, str], List[_DirectCallFunction]] = {}
    for fact in facts:
        if fact.fact_kind != "function":
            continue
        function = _direct_call_function_from_code_fact(fact)
        functions_by_id[function.object_id] = function
        functions_by_name.setdefault(function.object_name, []).append(function)
        source = _direct_call_function_canonical_source(function)
        if source is not None:
            functions_by_source_name.setdefault((source, function.object_name), []).append(function)
    return _DirectCallResolutionIndex(
        functions_by_id=functions_by_id,
        functions_by_name=functions_by_name,
        functions_by_source_name=functions_by_source_name,
    )


def _select_direct_call_target(
    index: _DirectCallResolutionIndex,
    evidence: DirectCallEvidence,
    caller: _DirectCallFunction,
    stats: _DirectCallResolutionStats,
) -> Optional[Tuple[_DirectCallFunction, str]]:
    caller_source = _direct_call_function_canonical_source(caller)
    if evidence.referenced_source:
        exact_candidates = index.functions_by_source_name.get((evidence.referenced_source, evidence.callee_name), [])
        if exact_candidates:
            return _unique_candidate_after_linkage_filter(
                exact_candidates,
                caller_source,
                stats,
                "exact_source",
                internal_on_empty=True,
            )

    fallback_candidates = index.functions_by_name.get(evidence.callee_name, [])
    if fallback_candidates:
        return _unique_candidate_after_linkage_filter(
            fallback_candidates,
            caller_source,
            stats,
            "unique_name",
            internal_on_empty=evidence.referenced_source is not None,
        )

    if evidence.referenced_source:
        stats.internal_unresolved_count += 1
    else:
        stats.external_unresolved_count += 1
    return None


def _unique_candidate_after_linkage_filter(
    candidates: Sequence[_DirectCallFunction],
    caller_source: Optional[str],
    stats: _DirectCallResolutionStats,
    strategy: str,
    *,
    internal_on_empty: bool,
) -> Optional[Tuple[_DirectCallFunction, str]]:
    filtered: List[_DirectCallFunction] = []
    for candidate in candidates:
        candidate_source = _direct_call_function_canonical_source(candidate)
        if _is_internal_linkage_function(candidate) and caller_source is not None and candidate_source != caller_source:
            stats.linkage_filtered_count += 1
            continue
        filtered.append(candidate)
    if len(filtered) == 1:
        return filtered[0], strategy
    if len(filtered) > 1:
        stats.ambiguous_call_count += 1
        return None
    if internal_on_empty or len(filtered) != len(candidates):
        stats.internal_unresolved_count += 1
    else:
        stats.external_unresolved_count += 1
    return None


def _make_resolved_direct_call_relative(
    caller: _DirectCallFunction,
    target: _DirectCallFunction,
    evidence: DirectCallEvidence,
    strategy: str,
    profile: str,
) -> FactRelative:
    payload: Dict[str, JSONValue] = {
        "evidence_source": evidence.evidence_source,
        "callee_name": evidence.callee_name,
        "resolution_strategy": strategy,
    }
    if evidence.referenced_source is not None:
        payload["referenced_source"] = evidence.referenced_source
    identity = json.dumps(
        {
            "from": caller.object_id,
            "to": target.object_id,
            "kind": "direct_call",
            "condition": evidence.condition.to_json() if evidence.condition is not None else None,
            "payload": payload,
            "profile": profile,
            "source": evidence.evidence_source,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return FactRelative(
        relative_id=f"rel:direct_call:{_hash_text(identity)[:20]}",
        from_fact_id=caller.object_id,
        to_fact_id=target.object_id,
        relation_kind="direct_call",
        condition=evidence.condition,
        object_profile=profile,
        evidence_source=evidence.evidence_source,
        confidence=1.0,
        payload=payload,
    )


def _direct_call_function_from_code_fact(fact: CodeFact) -> _DirectCallFunction:
    linkage = fact.payload.get("linkage")
    return _DirectCallFunction(
        object_id=fact.object_id,
        object_name=fact.object_name,
        object_source=fact.object_source,
        canonical_source=_fact_canonical_source(fact),
        linkage=linkage if isinstance(linkage, str) and linkage else None,
    )


def _direct_call_function_canonical_source(function: _DirectCallFunction) -> Optional[str]:
    if function.canonical_source:
        return function.canonical_source
    if ":" in function.object_source:
        return function.object_source.rsplit(":", 1)[0]
    return function.object_source or None


def _is_internal_linkage_function(function: _DirectCallFunction) -> bool:
    return isinstance(function.linkage, str) and function.linkage.lower() in {"static", "internal"}


def _fact_canonical_source(fact: CodeFact) -> Optional[str]:
    source = fact.payload.get("canonical_source")
    if isinstance(source, str) and source:
        return source
    if ":" in fact.object_source:
        return fact.object_source.rsplit(":", 1)[0]
    return fact.object_source or None


def _is_internal_linkage_fact(fact: CodeFact) -> bool:
    linkage = fact.payload.get("linkage")
    return isinstance(linkage, str) and linkage.lower() in {"static", "internal"}


__all__ = [name for name in globals() if not name.startswith("__")]

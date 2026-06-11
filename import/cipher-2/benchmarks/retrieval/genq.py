"""Deterministic question selection utilities."""

from __future__ import annotations

import random
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from cipher2.storage import FactRecord, open_fact_store

from benchmarks.retrieval.models import GoldGraph, ProbeCase


def select_cases(cases: Iterable[ProbeCase], *, seed: int, limit: int, dimensions: Iterable[str]) -> List[ProbeCase]:
    allowed = set(dimensions)
    selected = [case for case in cases if not allowed or case.dimension in allowed]
    selected.sort(key=lambda case: (case.library, case.dimension, case.case_id))
    rng = random.Random(seed)
    rng.shuffle(selected)
    return selected[:limit]


def cases_from_gold(*, library: str, repo_root: Path, gold: GoldGraph, dimensions: Iterable[str]) -> List[ProbeCase]:
    allowed = set(dimensions)
    fact_by_name = _facts_by_name(repo_root)
    cases: List[ProbeCase] = []
    if not allowed or "CALLERS" in allowed:
        callers: Dict[str, List[str]] = defaultdict(list)
        for call in gold.calls:
            callers[call.callee].append(call.caller)
        for callee, names in sorted(callers.items()):
            target = _first_fact(fact_by_name, callee)
            if target is not None:
                cases.append(
                    ProbeCase(
                        case_id=f"{library}-callers-{callee}",
                        library=library,
                        dimension="CALLERS",
                        query=callee,
                        target_fact_id=target.object_id,
                        gold_answers=sorted(set(names)),
                    )
                )
    if not allowed or "CALLEES" in allowed:
        callees: Dict[str, List[str]] = defaultdict(list)
        for call in gold.calls:
            callees[call.caller].append(call.callee)
        for caller, names in sorted(callees.items()):
            target = _first_fact(fact_by_name, caller)
            if target is not None:
                cases.append(
                    ProbeCase(
                        case_id=f"{library}-callees-{caller}",
                        library=library,
                        dimension="CALLEES",
                        query=caller,
                        target_fact_id=target.object_id,
                        gold_answers=sorted(set(names)),
                    )
                )
    if not allowed or "FIELD_ACC" in allowed:
        accessors: Dict[str, List[str]] = defaultdict(list)
        for access in gold.field_accesses:
            accessors[access.field_name].append(access.accessor)
        for field_name, names in sorted(accessors.items()):
            target = _first_fact(fact_by_name, field_name)
            if target is not None:
                cases.append(
                    ProbeCase(
                        case_id=f"{library}-field-acc-{field_name}",
                        library=library,
                        dimension="FIELD_ACC",
                        query=field_name,
                        target_fact_id=target.object_id,
                        gold_answers=sorted(set(names)),
                    )
                )
    if not allowed or "DEFLOC" in allowed:
        for function in sorted(gold.functions, key=lambda item: item.name):
            target = _first_fact(fact_by_name, function.name)
            if target is not None:
                cases.append(
                    ProbeCase(
                        case_id=f"{library}-defloc-{function.name}",
                        library=library,
                        dimension="DEFLOC",
                        query=function.name,
                        target_fact_id=target.object_id,
                        gold_answers=[function.name],
                    )
                )
    return cases


def _facts_by_name(repo_root: Path) -> Dict[str, List[FactRecord]]:
    store = open_fact_store(repo_root, mode="r", log_enabled=False)
    grouped: Dict[str, List[FactRecord]] = defaultdict(list)
    for fact in store.iter_facts():
        grouped[fact.object_name].append(fact)
    for facts in grouped.values():
        facts.sort(key=lambda fact: (fact.payload.get("fact_kind") != "function", fact.object_id))
    return grouped


def _first_fact(fact_by_name: Dict[str, List[FactRecord]], name: str) -> Optional[FactRecord]:
    facts = fact_by_name.get(name)
    if not facts:
        return None
    return facts[0]

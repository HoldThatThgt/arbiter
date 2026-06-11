"""Store-side coverage and full-ceiling helpers for retrieval benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Set

from cipher2.storage import FactRecord, FactRelative, open_fact_store

from benchmarks.retrieval.models import ProbeCase, RepoSpec, unique_sorted


CALL_RELATIONS = {"direct_call", "dispatches_via"}
FIELD_ACCESS_RELATIONS = {"field_read", "field_write"}


@dataclass(frozen=True)
class SnapshotValidation:
    ok: bool
    reason: Optional[str]
    current_snapshot_id: Optional[str]
    expected_snapshot_id: str
    snapshot_path: Path


def validate_snapshot(repo: RepoSpec) -> SnapshotValidation:
    current_pointer = repo.repo_root / ".cipher" / "snapshots" / "current"
    if not current_pointer.exists():
        return SnapshotValidation(False, "snapshot_missing", None, repo.snapshot_id, repo.snapshot_path)
    current_id = current_pointer.read_text(encoding="utf-8").strip()
    if current_id != repo.snapshot_id:
        return SnapshotValidation(False, "snapshot_mismatch", current_id, repo.snapshot_id, repo.snapshot_path)
    if not repo.snapshot_path.exists() or not repo.snapshot_path.is_dir():
        return SnapshotValidation(False, "snapshot_path_missing", current_id, repo.snapshot_id, repo.snapshot_path)
    return SnapshotValidation(True, None, current_id, repo.snapshot_id, repo.snapshot_path)


def full_answers_for_case(repo_root: Path, case: ProbeCase) -> List[str]:
    store = open_fact_store(repo_root, mode="r", log_enabled=False)
    if case.dimension == "DEFLOC":
        if case.target_fact_id:
            fact = store.get_fact(case.target_fact_id)
            return _fact_labels(fact) if fact is not None else []
        return _search_labels(store.search(case.query, limit=50))
    if not case.target_fact_id:
        return _search_labels(store.search(case.query, limit=50))
    relatives = list(store.iter_relatives())
    answers: Set[str] = set()
    for relative in relatives:
        if not _relative_matches_case(relative, case):
            continue
        endpoint_id = relative.from_fact_id if relative.to_fact_id == case.target_fact_id else relative.to_fact_id
        fact = store.get_fact(endpoint_id)
        answers.update(_fact_labels(fact))
        answers.add(endpoint_id)
    return unique_sorted(answers)


def store_endpoint_names(repo_root: Path, fact_ids: Iterable[str]) -> List[str]:
    store = open_fact_store(repo_root, mode="r", log_enabled=False)
    answers: Set[str] = set()
    for fact_id in fact_ids:
        fact = store.get_fact(fact_id)
        answers.update(_fact_labels(fact))
    return unique_sorted(answers)


def _relative_matches_case(relative: FactRelative, case: ProbeCase) -> bool:
    target = case.target_fact_id
    if target is None:
        return False
    if case.dimension == "CALLERS":
        return relative.relation_kind in CALL_RELATIONS and relative.to_fact_id == target
    if case.dimension == "CALLEES":
        return relative.relation_kind in CALL_RELATIONS and relative.from_fact_id == target
    if case.dimension == "FIELD_ACC":
        return relative.relation_kind in FIELD_ACCESS_RELATIONS and relative.to_fact_id == target
    return relative.from_fact_id == target or relative.to_fact_id == target


def _fact_labels(fact: Optional[FactRecord]) -> List[str]:
    if fact is None:
        return []
    labels = [fact.object_id, fact.object_name, fact.object_profile, fact.object_source]
    return [label for label in labels if label]


def _search_labels(facts: Iterable[FactRecord]) -> List[str]:
    labels: List[str] = []
    for fact in facts:
        labels.extend(_fact_labels(fact))
    return unique_sorted(labels)

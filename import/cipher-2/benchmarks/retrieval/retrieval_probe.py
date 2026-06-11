"""Replay MCP-visible retrieval output and score recoverability."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set

from cipher2.mcp import McpError, open_mcp_server
from cipher2.storage import StorageError, open_fact_store

from benchmarks.retrieval.ast_gold import build_gold_graph
from benchmarks.retrieval.analyze import root_cause_buckets
from benchmarks.retrieval.coverage_pool import full_answers_for_case, validate_snapshot
from benchmarks.retrieval.genq import cases_from_gold, select_cases
from benchmarks.retrieval.manifest import load_manifest
from benchmarks.retrieval.models import (
    CaseProbeResult,
    EvalCase,
    ProbeMetric,
    RepoSpec,
    RetrievalBenchmarkError,
    RetrievalManifest,
    RetrievalRunSummary,
    unique_sorted,
)
from benchmarks.retrieval.score import aggregate_probe_metrics, coverage_records_from_results, recover_ratio


SEARCH_LIMIT = 20


@dataclass(frozen=True)
class ProbeEvidence:
    case: EvalCase
    preview_answers: List[str]
    full_answers: List[str]
    skipped_reason: Optional[str] = None

    @property
    def preview_score(self) -> float:
        return recover_ratio(self.case.gold_answers, self.preview_answers)

    @property
    def full_score(self) -> float:
        return recover_ratio(self.case.gold_answers, self.full_answers)

    def to_json(self) -> dict:
        return {
            "case": self.case.to_json(),
            "preview_answers": list(self.preview_answers),
            "full_answers": list(self.full_answers),
            "preview_score": self.preview_score,
            "full_score": self.full_score,
            "skipped_reason": self.skipped_reason,
        }


def run_probe(
    manifest: RetrievalManifest,
    *,
    budget: str = "normal",
    repo_root_filter: Optional[Path] = None,
) -> RetrievalRunSummary:
    if budget not in {"small", "normal", "large"}:
        raise RetrievalBenchmarkError("invalid_budget", "budget must be small, normal, or large")
    results: List[CaseProbeResult] = []
    metrics: List[ProbeMetric] = []
    skipped: List[dict] = []
    libraries: List[str] = []
    for repo in manifest.repositories:
        if repo_root_filter is not None and repo.repo_root.resolve() != repo_root_filter.resolve():
            continue
        libraries.append(repo.name)
        validation = validate_snapshot(repo)
        if not validation.ok:
            skipped.append({"library": repo.name, "reason": validation.reason or "snapshot_invalid"})
            metrics.extend(_skipped_metrics(repo.name, manifest.dimensions, validation.reason or "snapshot_invalid"))
            continue
        try:
            repo_cases = _repo_cases(manifest, repo)
        except RetrievalBenchmarkError as exc:
            skipped.append({"library": repo.name, "reason": exc.code})
            metrics.extend(_skipped_metrics(repo.name, manifest.dimensions, exc.code))
            continue
        cases = select_cases(repo_cases, seed=manifest.seed, limit=manifest.case_limit, dimensions=manifest.dimensions)
        if not cases:
            skipped.append({"library": repo.name, "reason": "empty_case_pool"})
            metrics.extend(_skipped_metrics(repo.name, manifest.dimensions, "empty_case_pool"))
            continue
        for case in cases:
            results.append(probe_case(repo, case, budget=budget))
    metrics.extend(aggregate_probe_metrics(results))
    return RetrievalRunSummary(
        run_id=_run_id(manifest, budget),
        libraries=libraries,
        metrics=sorted(metrics, key=lambda item: (item.library, item.dimension, item.skip_reason or "")),
        root_causes=root_cause_buckets(results),
        coverage=coverage_records_from_results(results),
        cases=results,
        skipped=skipped,
    )


def probe_case(repo: RepoSpec, case, *, budget: str = "normal") -> CaseProbeResult:
    preview_answers, detail_attempted, detail_failed = _preview_answers(repo, case, budget=budget)
    full_answers = full_answers_for_case(repo.repo_root, case)
    preview_recovered = recover_ratio(case.gold_answers, preview_answers)
    full_recovered = recover_ratio(case.gold_answers, full_answers)
    return CaseProbeResult(
        case_id=case.case_id,
        library=case.library,
        dimension=case.dimension,
        preview_recovered=round(preview_recovered, 6),
        full_recovered=round(full_recovered, 6),
        preview_answers=preview_answers,
        full_answers=full_answers,
        gold_answers=list(case.gold_answers),
        root_cause=_root_cause(
            preview_recovered=preview_recovered,
            full_recovered=full_recovered,
            detail_attempted=detail_attempted,
            detail_failed=detail_failed,
        ),
    )


def probe_retest_case(target_repo: Path, case: EvalCase, *, budget: str = "normal") -> ProbeEvidence:
    preview_answers: Set[str] = set()
    full_answers: Set[str] = set()
    try:
        server = open_mcp_server(target_repo, log_enabled=False)
        search = server.search(case.query, limit=SEARCH_LIMIT)
        for result in search.results:
            preview_answers.update(_summary_labels(result))
        detail_fact_id = case.target_fact_id or (search.results[0].object_id if search.results else None)
        if detail_fact_id:
            try:
                detail = server.detail(detail_fact_id, budget=budget)
                preview_answers.update(_summary_labels(detail.fact))
                for relative in detail.relative_preview.relatives:
                    preview_answers.update(_relative_labels(relative))
            except McpError:
                pass
        if case.target_fact_id:
            full_answers.update(_full_answers_for_fact(target_repo, case.target_fact_id))
        full_answers.update(preview_answers)
        return ProbeEvidence(
            case=case,
            preview_answers=unique_sorted(preview_answers),
            full_answers=unique_sorted(full_answers),
        )
    except (McpError, StorageError, OSError) as exc:
        return ProbeEvidence(case=case, preview_answers=[], full_answers=[], skipped_reason=type(exc).__name__)


def probe_cases(target_repo: Path, cases: Iterable[EvalCase], *, budget: str = "normal") -> List[ProbeEvidence]:
    return [probe_retest_case(target_repo, case, budget=budget) for case in cases]


def _repo_cases(manifest: RetrievalManifest, repo: RepoSpec) -> List:
    if repo.cases:
        return list(repo.cases)
    if not repo.gold_sources:
        return []
    gold = build_gold_graph(
        repo_root=repo.repo_root,
        clang_executable=manifest.clang_executable,
        sources=repo.gold_sources,
        clang_args=repo.clang_args,
    )
    return cases_from_gold(library=repo.name, repo_root=repo.repo_root, gold=gold, dimensions=manifest.dimensions)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline retrieval preview/full probe.")
    parser.add_argument("repo_snapshot", type=Path, help="initialized target repository root with .cipher/snapshots/current")
    parser.add_argument("--manifest", type=Path, required=True, help="JSON/YAML retrieval benchmark manifest")
    parser.add_argument("--budget", choices=["small", "normal", "large"], default="normal", help="MCP detail budget")
    parser.add_argument("--output", type=Path, help="optional JSON output path")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        summary = run_probe(manifest, budget=args.budget, repo_root_filter=args.repo_snapshot)
    except RetrievalBenchmarkError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False), file=sys.stderr)
        return 2
    payload = summary.to_json()
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            print(
                json.dumps({"error": {"code": "output_write_failed", "message": str(exc)}}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 2
    print(rendered)
    return 0


def _preview_answers(repo: RepoSpec, case, *, budget: str) -> tuple:
    answers: Set[str] = set()
    detail_attempted = False
    detail_failed = False
    try:
        server = open_mcp_server(repo.repo_root, log_enabled=False)
        search = server.search(case.query, limit=SEARCH_LIMIT)
        search_ids = [item.object_id for item in search.results]
        for item in search.results:
            answers.update(_summary_labels(item))
        detail_id = None
        if case.target_fact_id and case.target_fact_id in search_ids:
            detail_id = case.target_fact_id
        elif not case.target_fact_id and search_ids:
            detail_id = search_ids[0]
        if detail_id:
            detail_attempted = True
            detail = server.detail(detail_id, budget=budget)
            answers.update(_summary_labels(detail.fact))
            for relative in detail.relative_preview.relatives:
                answers.update(_relative_labels(relative))
    except McpError:
        detail_failed = detail_attempted
    return unique_sorted(answers), detail_attempted, detail_failed


def _summary_labels(summary) -> List[str]:
    values = [
        getattr(summary, "object_id", None),
        getattr(summary, "object_name", None),
        getattr(summary, "object_profile", None),
        getattr(summary, "object_source", None),
    ]
    return [value for value in values if isinstance(value, str) and value]


def _relative_labels(relative) -> List[str]:
    values = [
        getattr(relative, "endpoint_name", None),
        getattr(relative, "endpoint_profile", None),
        getattr(relative, "endpoint_source", None),
        getattr(relative, "from_fact_id", None),
        getattr(relative, "to_fact_id", None),
    ]
    return [value for value in values if isinstance(value, str) and value]


def _full_answers_for_fact(target_repo: Path, fact_id: str) -> List[str]:
    store = open_fact_store(target_repo, mode="r", log_enabled=False)
    answers: Set[str] = set()
    fact = store.get_fact(fact_id)
    if fact is not None:
        answers.add(fact.object_id)
        answers.add(fact.object_name)
        answers.add(fact.object_profile)
    for relative in store.iter_relatives():
        endpoint_id = None
        if relative.to_fact_id == fact_id:
            endpoint_id = relative.from_fact_id
        elif relative.from_fact_id == fact_id:
            endpoint_id = relative.to_fact_id
        if endpoint_id is None:
            continue
        endpoint = store.get_fact(endpoint_id)
        if endpoint is not None:
            answers.add(endpoint.object_id)
            answers.add(endpoint.object_name)
            answers.add(endpoint.object_profile)
    return unique_sorted(answers)


def _root_cause(*, preview_recovered: float, full_recovered: float, detail_attempted: bool, detail_failed: bool) -> str:
    if full_recovered == 0:
        return "missing_fact_or_relative"
    if preview_recovered >= full_recovered:
        return "recovered"
    if detail_failed:
        return "mcp_detail_failed"
    if not detail_attempted:
        return "search_miss"
    if preview_recovered == 0:
        return "preview_truncated_or_endpoint_missing"
    return "preview_partial"


def _skipped_metrics(library: str, dimensions: Iterable[str], reason: str) -> List[ProbeMetric]:
    return [
        ProbeMetric(
            library=library,
            dimension=dimension,
            case_count=0,
            recover_preview=0.0,
            recover_full=0.0,
            bound_loss=0.0,
            skip_reason=reason,
        )
        for dimension in sorted(set(dimensions) | {"ALL"})
    ]


def _run_id(manifest: RetrievalManifest, budget: str) -> str:
    payload = json.dumps(manifest.to_json(), ensure_ascii=False, sort_keys=True, default=str)
    return "retrieval-" + hashlib.sha256((payload + "\n" + budget).encode("utf-8")).hexdigest()[:12]


if __name__ == "__main__":
    raise SystemExit(main())

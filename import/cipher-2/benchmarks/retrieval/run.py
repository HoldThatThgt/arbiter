"""Repository-level retrieval benchmark orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from benchmarks.retrieval.analyze import render_markdown as render_retrieval_markdown
from benchmarks.retrieval.analyze import root_cause_buckets
from benchmarks.retrieval.manifest import load_baselines, load_manifest
from benchmarks.retrieval.models import (
    CoverageRecord,
    EvalCase,
    EvalRunSummary,
    ModelPlan,
    ModelPrediction,
    ModelRequest,
    ProbeMetric,
    RetestManifest,
    RetrievalBenchmarkError,
    RetrievalManifest,
    RetrievalRunSummary,
)
from benchmarks.retrieval.retrieval_probe import ProbeEvidence, probe_cases, run_probe
from benchmarks.retrieval.score import aggregate_ab, aggregate_retrieval, score_prediction


def run_manifest(
    manifest,
    *,
    budget: str = "normal",
    max_workers: int = 1,
    mode: str = "probe",
    output: Optional[Path] = None,
    code_revision_label: str = "working-tree",
):
    if isinstance(manifest, RetestManifest):
        summary = _run_retest_manifest(
            manifest,
            mode=mode,
            budget=budget,
            max_workers=max_workers,
            code_revision_label=code_revision_label,
        )
        if output is not None:
            write_outputs(summary, output)
        return summary
    if isinstance(manifest, RetrievalManifest):
        summary = _run_retrieval_manifest(manifest, budget=budget, max_workers=max_workers)
        if output is not None:
            write_outputs(summary, output)
        return summary
    raise RetrievalBenchmarkError("invalid_manifest", "unsupported retrieval manifest type")


def _run_retrieval_manifest(
    manifest: RetrievalManifest,
    *,
    budget: str = "normal",
    max_workers: int = 1,
) -> RetrievalRunSummary:
    if not isinstance(max_workers, int) or max_workers < 1:
        raise RetrievalBenchmarkError("invalid_max_workers", "max_workers must be >= 1")
    if max_workers == 1 or len(manifest.repositories) <= 1:
        return run_probe(manifest, budget=budget)
    summaries: List[RetrievalRunSummary] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for repo in manifest.repositories:
            repo_manifest = replace(manifest, repositories=[repo])
            futures.append(executor.submit(run_probe, repo_manifest, budget=budget))
        for future in futures:
            summaries.append(future.result())
    return _merge_summaries(summaries)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline retrieval benchmark harness.")
    parser.add_argument("--manifest", type=Path, required=True, help="JSON/YAML retrieval benchmark manifest")
    parser.add_argument("--budget", choices=["small", "normal", "large"], default="normal", help="MCP detail budget")
    parser.add_argument("--output", type=Path, required=True, help="output directory or .json/.md file")
    parser.add_argument("--max-workers", type=int, default=1, help="repository-level worker count")
    parser.add_argument("--baseline", type=Path, help="optional retest baseline JSON/YAML")
    parser.add_argument("--mode", choices=["probe", "ab", "all"], default="probe", help="retest mode")
    parser.add_argument("--code-revision-label", default="working-tree", help="label for retest reports")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if isinstance(manifest, RetestManifest) and args.baseline is not None:
            manifest = replace(manifest, baselines=load_baselines(args.baseline))
        summary = run_manifest(
            manifest,
            budget=args.budget,
            max_workers=args.max_workers,
            mode=args.mode,
            output=args.output,
            code_revision_label=args.code_revision_label,
        )
    except RetrievalBenchmarkError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False), file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            json.dumps({"error": {"code": "output_write_failed", "message": str(exc)}}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    return 0


def write_outputs(summary, output: Path) -> None:
    payload = json.dumps(summary.to_json(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    markdown = render_retest_markdown(summary) if isinstance(summary, EvalRunSummary) else render_retrieval_markdown(summary)
    if output.suffix == ".json":
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        output.with_suffix(".md").write_text(markdown, encoding="utf-8")
        return
    if output.suffix == ".md":
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
        output.with_suffix(".json").write_text(payload, encoding="utf-8")
        return
    output.mkdir(parents=True, exist_ok=True)
    (output / "run_summary.json").write_text(payload, encoding="utf-8")
    (output / "report.md").write_text(markdown, encoding="utf-8")


def _merge_summaries(summaries: List[RetrievalRunSummary]) -> RetrievalRunSummary:
    libraries: List[str] = []
    metrics: List[ProbeMetric] = []
    coverage: List[CoverageRecord] = []
    cases = []
    skipped = []
    for summary in summaries:
        libraries.extend(summary.libraries)
        metrics.extend(summary.metrics)
        coverage.extend(summary.coverage)
        cases.extend(summary.cases)
        skipped.extend(summary.skipped)
    run_id = summaries[0].run_id if summaries else "retrieval-empty"
    return RetrievalRunSummary(
        run_id=run_id,
        libraries=libraries,
        metrics=sorted(metrics, key=lambda item: (item.library, item.dimension, item.skip_reason or "")),
        root_causes=root_cause_buckets(cases),
        coverage=coverage,
        cases=cases,
        skipped=skipped,
    )


def _run_retest_manifest(
    manifest: RetestManifest,
    *,
    mode: str = "probe",
    budget: str = "normal",
    max_workers: int = 1,
    code_revision_label: str = "working-tree",
) -> EvalRunSummary:
    if mode not in {"probe", "ab", "all"}:
        raise RetrievalBenchmarkError("invalid_mode", "mode must be probe, ab, or all")
    if not isinstance(max_workers, int) or max_workers < 1:
        raise RetrievalBenchmarkError("invalid_max_workers", "max_workers must be >= 1")
    if max_workers != 1:
        raise RetrievalBenchmarkError("unsupported_max_workers", "retest mode currently requires --max-workers 1")

    skipped: List[dict] = []
    retrieval_scores: List[Tuple[EvalCase, float, float]] = []
    evidence_by_case: Dict[str, ProbeEvidence] = {}

    if mode in {"probe", "all", "ab"}:
        for library in manifest.libraries:
            repo = Path(library.repo)
            if not repo.exists():
                skipped.append({"library": library.name, "reason": "repo_missing", "repo": library.repo})
                continue
            for evidence in probe_cases(repo, library.cases, budget=budget):
                evidence_by_case[evidence.case.case_id] = evidence
                if evidence.skipped_reason:
                    skipped.append(
                        {
                            "library": library.name,
                            "case_id": evidence.case.case_id,
                            "reason": evidence.skipped_reason,
                        }
                    )
                    continue
                retrieval_scores.append((evidence.case, evidence.preview_score, evidence.full_score))

    weak_model_ab = []
    if mode in {"ab", "all"}:
        if manifest.model_plan is None or not manifest.model_plan.enabled:
            skipped.append({"library": "*", "reason": "model_plan_disabled"})
        else:
            ab_scores, ab_skipped = _run_ab(manifest, manifest.model_plan, evidence_by_case)
            skipped.extend(ab_skipped)
            weak_model_ab = aggregate_ab(ab_scores)

    return EvalRunSummary(
        run_id=f"retest-{int(time.time())}",
        code_revision_label=code_revision_label,
        libraries=[library.name for library in manifest.libraries],
        retrieval=aggregate_retrieval(retrieval_scores, baselines=manifest.baselines),
        weak_model_ab=weak_model_ab,
        skipped=skipped,
    )


def render_retest_markdown(summary: EvalRunSummary) -> str:
    lines = [
        "# 检索可还原率复测报告",
        "",
        f"- run_id: `{summary.run_id}`",
        f"- code_revision_label: `{summary.code_revision_label}`",
        f"- libraries: {', '.join(summary.libraries) if summary.libraries else '-'}",
        "",
        "## Retrieval Probe",
        "",
        "| library | dimension | cases | recover@preview | recover@full | preview_gap | ceiling_delta | skipped |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for metric in summary.retrieval:
        lines.append(
            "| {library} | {dimension} | {cases} | {preview:.3f} | {full:.3f} | {gap:.3f} | {ceiling:.3f} | {skipped} |".format(
                library=metric.library,
                dimension=metric.dimension,
                cases=metric.case_count,
                preview=metric.recover_preview,
                full=metric.recover_full,
                gap=metric.preview_gap,
                ceiling=metric.ceiling_delta,
                skipped=metric.skipped_reason or "",
            )
        )
    lines.extend(
        [
            "",
            "## Weak Model A/B",
            "",
            "| library | dimension | cases | acc_B | acc_C | delta | rescue | skipped |",
            "|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for metric in summary.weak_model_ab:
        lines.append(
            "| {library} | {dimension} | {cases} | {acc_b:.3f} | {acc_c:.3f} | {delta:.3f} | {rescue:.3f} | {skipped} |".format(
                library=metric.library,
                dimension=metric.dimension,
                cases=metric.case_count,
                acc_b=metric.acc_b,
                acc_c=metric.acc_c,
                delta=metric.delta,
                rescue=metric.rescue,
                skipped=metric.skipped_reason or "",
            )
        )
    if summary.skipped:
        lines.extend(["", "## Skipped", ""])
        for row in summary.skipped:
            lines.append(f"- `{row.get('library', '?')}` `{row.get('case_id', '-')}`: {row.get('reason', 'unknown')}")
    lines.append("")
    return "\n".join(lines)


def _run_ab(
    manifest: RetestManifest,
    plan: ModelPlan,
    evidence_by_case: Dict[str, ProbeEvidence],
) -> Tuple[List[Tuple[EvalCase, float, float]], List[dict]]:
    missing_env = [name for name in plan.required_env if name not in os.environ]
    if missing_env:
        return [], [{"library": "*", "reason": "missing_model_env", "env": ",".join(sorted(missing_env))}]
    scores: List[Tuple[EvalCase, float, float]] = []
    skipped: List[dict] = []
    consumed = 0
    for library in manifest.libraries:
        for case in library.cases:
            if plan.max_cases is not None and consumed >= plan.max_cases:
                return scores, skipped
            evidence = evidence_by_case.get(case.case_id)
            if evidence is None:
                skipped.append({"library": library.name, "case_id": case.case_id, "reason": "probe_missing"})
                continue
            if evidence.skipped_reason:
                skipped.append({"library": library.name, "case_id": case.case_id, "reason": evidence.skipped_reason})
                continue
            grep = ModelRequest(
                case_id=case.case_id,
                condition="grep",
                question=case.question,
                grep_context=case.grep_context,
                cipher_context=None,
            )
            grep_cipher = ModelRequest(
                case_id=case.case_id,
                condition="grep_cipher",
                question=case.question,
                grep_context=case.grep_context,
                cipher_context=_cipher_context(evidence),
            )
            try:
                grep_prediction = _invoke_adapter(plan, grep)
                cipher_prediction = _invoke_adapter(plan, grep_cipher)
            except RetrievalBenchmarkError as exc:
                skipped.append({"library": library.name, "case_id": case.case_id, "reason": exc.code})
                continue
            scores.append((case, score_prediction(case, grep_prediction), score_prediction(case, cipher_prediction)))
            consumed += 1
    return scores, skipped


def _invoke_adapter(plan: ModelPlan, request: ModelRequest) -> ModelPrediction:
    try:
        completed = subprocess.run(
            plan.command,
            input=json.dumps(request.to_json(), ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=plan.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetrievalBenchmarkError("adapter_error", str(exc)) from exc
    if completed.returncode != 0:
        raise RetrievalBenchmarkError("adapter_error", completed.stderr.strip() or "adapter failed")
    try:
        row = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RetrievalBenchmarkError("adapter_invalid_json", "adapter returned invalid JSON") from exc
    prediction = ModelPrediction.from_json(row)
    if prediction.case_id != request.case_id or prediction.condition != request.condition:
        raise RetrievalBenchmarkError("adapter_mismatched_response", "adapter returned a mismatched case or condition")
    return prediction


def _cipher_context(evidence: ProbeEvidence) -> dict:
    return {
        "preview_answers": list(evidence.preview_answers),
        "full_answers": list(evidence.full_answers),
        "preview_score": evidence.preview_score,
        "full_score": evidence.full_score,
    }


if __name__ == "__main__":
    raise SystemExit(main())

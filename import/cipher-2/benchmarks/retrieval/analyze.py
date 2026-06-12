"""Report rendering and root-cause aggregation for retrieval benchmarks."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable, List

from benchmarks.retrieval.models import CaseProbeResult, RetrievalRunSummary, RootCauseBucket


def root_cause_buckets(results: Iterable[CaseProbeResult]) -> List[RootCauseBucket]:
    examples = defaultdict(list)
    counts = Counter()
    for result in results:
        counts[result.root_cause] += 1
        if len(examples[result.root_cause]) < 5:
            examples[result.root_cause].append(result.case_id)
    return [
        RootCauseBucket(bucket=bucket, case_count=count, examples=examples[bucket])
        for bucket, count in sorted(counts.items())
    ]


def render_markdown(summary: RetrievalRunSummary) -> str:
    lines = [
        "# Retrieval Benchmark Report",
        "",
        f"- run_id: `{summary.run_id}`",
        f"- libraries: {', '.join(summary.libraries) if summary.libraries else '(none)'}",
        "",
        "## Metrics",
        "",
        "| library | dimension | cases | recover@preview | recover@full | bound_loss | skip |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for metric in summary.metrics:
        lines.append(
            "| {library} | {dimension} | {cases} | {preview:.6f} | {full:.6f} | {loss:.6f} | {skip} |".format(
                library=metric.library,
                dimension=metric.dimension,
                cases=metric.case_count,
                preview=metric.recover_preview,
                full=metric.recover_full,
                loss=metric.bound_loss,
                skip=metric.skip_reason or "",
            )
        )
    lines.extend(["", "## Coverage", "", "| library | dimension | covered | gold | precision |", "|---|---|---:|---:|---:|"])
    for record in summary.coverage:
        lines.append(
            f"| {record.library} | {record.dimension} | {record.covered_count} | {record.gold_count} | {record.precision:.6f} |"
        )
    lines.extend(["", "## Root Causes", "", "| bucket | cases | examples |", "|---|---:|---|"])
    for bucket in summary.root_causes:
        lines.append(f"| {bucket.bucket} | {bucket.case_count} | {', '.join(bucket.examples)} |")
    if summary.skipped:
        lines.extend(["", "## Skipped", "", "| library | reason |", "|---|---|"])
        for item in summary.skipped:
            lines.append(f"| {item.get('library', '')} | {item.get('reason', '')} |")
    return "\n".join(lines) + "\n"

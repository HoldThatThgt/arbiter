"""Scoring helpers for retrieval benchmark cases."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from benchmarks.retrieval.models import (
    BaselineMetric,
    CaseProbeResult,
    CoverageRecord,
    EvalCase,
    ModelPrediction,
    ProbeMetric,
    RetrievalMetric,
    WeakModelABMetric,
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.$:/#-]+")


def normalize_answer(value: str) -> str:
    return " ".join(token.casefold() for token in _TOKEN_RE.findall(value or ""))


def recover_ratio(gold_answers: Sequence[str], observed_answers: Iterable[str]) -> float:
    gold = {normalize_answer(answer) for answer in gold_answers if normalize_answer(answer)}
    if not gold:
        return 0.0
    observed = {normalize_answer(answer) for answer in observed_answers if normalize_answer(answer)}
    recovered = sum(1 for answer in gold if answer in observed)
    return recovered / len(gold)


def aggregate_probe_metrics(results: Sequence[CaseProbeResult]) -> List[ProbeMetric]:
    grouped: Dict[tuple, List[CaseProbeResult]] = defaultdict(list)
    for result in results:
        grouped[(result.library, result.dimension)].append(result)
        grouped[(result.library, "ALL")].append(result)
    metrics: List[ProbeMetric] = []
    for (library, dimension), items in sorted(grouped.items()):
        preview = sum(item.preview_recovered for item in items) / len(items)
        full = sum(item.full_recovered for item in items) / len(items)
        metrics.append(
            ProbeMetric(
                library=library,
                dimension=dimension,
                case_count=len(items),
                recover_preview=round(preview, 6),
                recover_full=round(full, 6),
                bound_loss=round(full - preview, 6),
            )
        )
    return metrics


def coverage_records_from_results(results: Sequence[CaseProbeResult]) -> List[CoverageRecord]:
    grouped: Dict[tuple, List[CaseProbeResult]] = defaultdict(list)
    for result in results:
        grouped[(result.library, result.dimension)].append(result)
    records: List[CoverageRecord] = []
    for (library, dimension), items in sorted(grouped.items()):
        covered_count = sum(1 for item in items if item.full_recovered > 0)
        records.append(
            CoverageRecord(
                library=library,
                dimension=dimension,
                covered_count=covered_count,
                gold_count=len(items),
                precision=1.0 if covered_count else 0.0,
            )
        )
    return records


def score_prediction(case: EvalCase, prediction: ModelPrediction) -> float:
    if prediction.abstained or prediction.error_code:
        return 0.0
    return recover_ratio(case.gold_answers, prediction.answer_names)


def aggregate_retrieval(
    case_scores: Iterable[Tuple[EvalCase, float, float]],
    *,
    baselines: Sequence[BaselineMetric],
) -> List[RetrievalMetric]:
    grouped: Dict[Tuple[str, str], List[Tuple[float, float]]] = defaultdict(list)
    for case, preview, full in case_scores:
        grouped[(case.library, case.dimension)].append((preview, full))
        grouped[(case.library, "ALL")].append((preview, full))
    metrics: List[RetrievalMetric] = []
    for (library, dimension), values in sorted(grouped.items()):
        preview_avg = _avg(preview for preview, _full in values)
        full_avg = _avg(full for _preview, full in values)
        baseline = _find_baseline(baselines, library, dimension)
        ceiling_delta = full_avg - baseline.full_before if baseline else 0.0
        metrics.append(
            RetrievalMetric(
                library=library,
                dimension=dimension,
                case_count=len(values),
                recover_preview=preview_avg,
                recover_full=full_avg,
                preview_gap=full_avg - preview_avg,
                ceiling_delta=ceiling_delta,
            )
        )
    return metrics


def aggregate_ab(case_scores: Iterable[Tuple[EvalCase, float, float]]) -> List[WeakModelABMetric]:
    grouped: Dict[Tuple[str, str], List[Tuple[float, float]]] = defaultdict(list)
    for case, grep_score, cipher_score in case_scores:
        grouped[(case.library, case.dimension)].append((grep_score, cipher_score))
        grouped[(case.library, "ALL")].append((grep_score, cipher_score))
    metrics: List[WeakModelABMetric] = []
    for (library, dimension), values in sorted(grouped.items()):
        acc_b = _avg(grep for grep, _cipher in values)
        acc_c = _avg(cipher for _grep, cipher in values)
        delta = acc_c - acc_b
        denominator = max(0.0, 1.0 - acc_b)
        rescue = delta / denominator if denominator else 0.0
        metrics.append(
            WeakModelABMetric(
                library=library,
                dimension=dimension,
                case_count=len(values),
                acc_b=acc_b,
                acc_c=acc_c,
                delta=delta,
                rescue=rescue,
            )
        )
    return metrics


def _find_baseline(baselines: Sequence[BaselineMetric], library: str, dimension: str) -> Optional[BaselineMetric]:
    for baseline in baselines:
        if baseline.library == library and baseline.dimension == dimension:
            return baseline
    return None


def _avg(values: Iterable[float]) -> float:
    collected = list(values)
    if not collected:
        return 0.0
    return sum(collected) / len(collected)

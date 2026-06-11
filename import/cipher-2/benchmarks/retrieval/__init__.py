"""Offline retrieval quality harness.

Only ``benchmarks.retrieval.run`` and ``benchmarks.retrieval.retrieval_probe``
are public ``python -m`` entrypoints. The other modules are import-only
building blocks.
"""

from benchmarks.retrieval.manifest import load_manifest
from benchmarks.retrieval.models import (
    BaselineMetric,
    EvalCase,
    EvalRunSummary,
    LibraryPlan,
    ModelPlan,
    ModelPrediction,
    ModelRequest,
    RetrievalMetric,
    RetestManifest,
    WeakModelABMetric,
)

__all__ = [
    "BaselineMetric",
    "EvalCase",
    "EvalRunSummary",
    "LibraryPlan",
    "ModelPlan",
    "ModelPrediction",
    "ModelRequest",
    "RetrievalMetric",
    "RetestManifest",
    "WeakModelABMetric",
    "analyze",
    "ast_gold",
    "coverage_pool",
    "genq",
    "load_manifest",
    "manifest",
    "models",
    "score",
]

"""Manifest loading for retrieval benchmark runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from benchmarks.retrieval.models import BaselineMetric, RetrievalBenchmarkError, RetrievalManifest, RetestManifest


def load_manifest(path: Path):
    manifest_path = Path(path)
    if not manifest_path.exists() or not manifest_path.is_file():
        raise RetrievalBenchmarkError("manifest_missing", f"manifest not found: {manifest_path}")
    text = manifest_path.read_text(encoding="utf-8")
    data = _parse_manifest_text(text, manifest_path)
    if "repositories" in data:
        return RetrievalManifest.from_json(data, manifest_dir=manifest_path.parent)
    if "libraries" in data:
        return RetestManifest.from_json(data)
    raise RetrievalBenchmarkError("invalid_manifest", "manifest must contain repositories or libraries")


def load_baselines(path: Path) -> list[BaselineMetric]:
    manifest_path = Path(path)
    if not manifest_path.exists() or not manifest_path.is_file():
        raise RetrievalBenchmarkError("baseline_missing", f"baseline file not found: {manifest_path}")
    data = _parse_manifest_text(manifest_path.read_text(encoding="utf-8"), manifest_path, require_mapping=False)
    rows = data.get("baselines") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RetrievalBenchmarkError("invalid_baseline", "baseline file must contain baselines")
    return [BaselineMetric.from_json(item) for item in rows]


def _parse_manifest_text(text: str, path: Path, *, require_mapping: bool = True) -> Any:
    suffix = path.suffix.casefold()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RetrievalBenchmarkError("yaml_unavailable", "YAML manifest requires PyYAML") from exc
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RetrievalBenchmarkError("manifest_invalid_json", f"invalid manifest JSON: {exc}") from exc
    if require_mapping and not isinstance(data, dict):
        raise RetrievalBenchmarkError("invalid_manifest", "manifest root must be an object")
    return data

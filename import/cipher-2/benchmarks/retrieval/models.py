"""Shared data models for the retrieval benchmark harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

JSONDict = Dict[str, Any]


class RetrievalBenchmarkError(Exception):
    """Raised when a retrieval benchmark input or run is invalid."""

    def __init__(self, code: str, message: Optional[str] = None) -> None:
        message = code if message is None else message
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProbeCase:
    case_id: str
    library: str
    dimension: str
    query: str
    gold_answers: List[str]
    target_fact_id: Optional[str] = None
    question: Optional[str] = None
    grep_context: List[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, row: Dict[str, Any], *, library: str) -> "ProbeCase":
        if not isinstance(row, dict):
            raise RetrievalBenchmarkError("invalid_case", "case row must be an object")
        required = ("case_id", "dimension", "query", "gold_answers")
        missing = [key for key in required if key not in row]
        if missing:
            raise RetrievalBenchmarkError("invalid_case", "case missing fields: " + ", ".join(missing))
        gold_answers = row["gold_answers"]
        if not isinstance(gold_answers, list) or any(not isinstance(item, str) or not item for item in gold_answers):
            raise RetrievalBenchmarkError("invalid_case", "gold_answers must be a non-empty string list")
        grep_context = row.get("grep_context") or []
        if not isinstance(grep_context, list) or any(not isinstance(item, str) for item in grep_context):
            raise RetrievalBenchmarkError("invalid_case", "grep_context must be a string list")
        return cls(
            case_id=_required_str(row, "case_id"),
            library=library,
            dimension=_required_str(row, "dimension"),
            query=_required_str(row, "query"),
            gold_answers=list(gold_answers),
            target_fact_id=_optional_str(row.get("target_fact_id"), "target_fact_id"),
            question=_optional_str(row.get("question"), "question"),
            grep_context=list(grep_context),
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "library": self.library,
            "dimension": self.dimension,
            "query": self.query,
            "gold_answers": list(self.gold_answers),
            "target_fact_id": self.target_fact_id,
            "question": self.question,
            "grep_context": list(self.grep_context),
        }


@dataclass(frozen=True)
class RepoSpec:
    name: str
    repo_root: Path
    snapshot_id: str
    snapshot_path: Path
    clang16_version: str
    cases: List[ProbeCase] = field(default_factory=list)
    gold_sources: List[str] = field(default_factory=list)
    clang_args: List[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, row: Dict[str, Any], *, manifest_dir: Path) -> "RepoSpec":
        if not isinstance(row, dict):
            raise RetrievalBenchmarkError("invalid_manifest", "repository entry must be an object")
        name = _required_str(row, "name")
        repo_root = _resolve_manifest_path(_required_str(row, "repo_root"), manifest_dir)
        snapshot_path_raw = _required_str(row, "snapshot_path")
        snapshot_path = Path(snapshot_path_raw)
        if not snapshot_path.is_absolute():
            snapshot_path = repo_root / snapshot_path
        raw_cases = row.get("cases", [])
        if not isinstance(raw_cases, list):
            raise RetrievalBenchmarkError("invalid_manifest", "cases must be a list")
        cases = [ProbeCase.from_json(case, library=name) for case in raw_cases]
        gold_sources = _string_list(row.get("gold_sources", []), "gold_sources")
        clang_args = _string_list(row.get("clang_args", []), "clang_args")
        return cls(
            name=name,
            repo_root=repo_root,
            snapshot_id=_required_str(row, "snapshot_id"),
            snapshot_path=snapshot_path,
            clang16_version=_required_str(row, "clang16_version"),
            cases=cases,
            gold_sources=gold_sources,
            clang_args=clang_args,
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "repo_root": str(self.repo_root),
            "snapshot_id": self.snapshot_id,
            "snapshot_path": str(self.snapshot_path),
            "clang16_version": self.clang16_version,
            "cases": [case.to_json() for case in self.cases],
            "gold_sources": list(self.gold_sources),
            "clang_args": list(self.clang_args),
        }


@dataclass(frozen=True)
class RetrievalManifest:
    repositories: List[RepoSpec]
    clang_executable: str
    seed: int
    dimensions: List[str]
    case_limit: int

    @classmethod
    def from_json(cls, row: Dict[str, Any], *, manifest_dir: Path) -> "RetrievalManifest":
        if not isinstance(row, dict):
            raise RetrievalBenchmarkError("invalid_manifest", "manifest must be an object")
        repositories = row.get("repositories")
        if not isinstance(repositories, list) or not repositories:
            raise RetrievalBenchmarkError("invalid_manifest", "repositories must be a non-empty list")
        seed = row.get("seed", 1)
        case_limit = row.get("case_limit", 100)
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise RetrievalBenchmarkError("invalid_manifest", "seed must be an integer")
        if not isinstance(case_limit, int) or isinstance(case_limit, bool) or case_limit < 1:
            raise RetrievalBenchmarkError("invalid_manifest", "case_limit must be a positive integer")
        return cls(
            repositories=[RepoSpec.from_json(repo, manifest_dir=manifest_dir) for repo in repositories],
            clang_executable=_required_str(row, "clang_executable"),
            seed=seed,
            dimensions=_string_list(row.get("dimensions", ["CALLERS", "CALLEES", "FIELD_ACC", "DEFLOC"]), "dimensions"),
            case_limit=case_limit,
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "repositories": [repo.to_json() for repo in self.repositories],
            "clang_executable": self.clang_executable,
            "seed": self.seed,
            "dimensions": list(self.dimensions),
            "case_limit": self.case_limit,
        }


@dataclass(frozen=True)
class GoldFunction:
    name: str
    source: str


@dataclass(frozen=True)
class GoldCall:
    caller: str
    callee: str
    source: str


@dataclass(frozen=True)
class GoldFieldAccess:
    accessor: str
    field_name: str
    access_kind: str
    source: str


@dataclass(frozen=True)
class GoldGraph:
    functions: List[GoldFunction] = field(default_factory=list)
    calls: List[GoldCall] = field(default_factory=list)
    field_accesses: List[GoldFieldAccess] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "functions": [function.__dict__ for function in self.functions],
            "calls": [call.__dict__ for call in self.calls],
            "field_accesses": [access.__dict__ for access in self.field_accesses],
        }


@dataclass(frozen=True)
class CoverageRecord:
    library: str
    dimension: str
    covered_count: int
    gold_count: int
    precision: float

    def to_json(self) -> Dict[str, Any]:
        return {
            "library": self.library,
            "dimension": self.dimension,
            "covered_count": self.covered_count,
            "gold_count": self.gold_count,
            "precision": self.precision,
        }


@dataclass(frozen=True)
class CaseProbeResult:
    case_id: str
    library: str
    dimension: str
    preview_recovered: float
    full_recovered: float
    preview_answers: List[str]
    full_answers: List[str]
    gold_answers: List[str]
    root_cause: str

    def to_json(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "library": self.library,
            "dimension": self.dimension,
            "preview_recovered": self.preview_recovered,
            "full_recovered": self.full_recovered,
            "preview_answers": list(self.preview_answers),
            "full_answers": list(self.full_answers),
            "gold_answers": list(self.gold_answers),
            "root_cause": self.root_cause,
        }


@dataclass(frozen=True)
class ProbeMetric:
    library: str
    dimension: str
    case_count: int
    recover_preview: float
    recover_full: float
    bound_loss: float
    skip_reason: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "library": self.library,
            "dimension": self.dimension,
            "case_count": self.case_count,
            "recover_preview": self.recover_preview,
            "recover_full": self.recover_full,
            "bound_loss": self.bound_loss,
            "skip_reason": self.skip_reason,
        }


@dataclass(frozen=True)
class RootCauseBucket:
    bucket: str
    case_count: int
    examples: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "bucket": self.bucket,
            "case_count": self.case_count,
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class RetrievalRunSummary:
    run_id: str
    libraries: List[str]
    metrics: List[ProbeMetric]
    root_causes: List[RootCauseBucket]
    coverage: List[CoverageRecord] = field(default_factory=list)
    cases: List[CaseProbeResult] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "libraries": list(self.libraries),
            "metrics": [metric.to_json() for metric in self.metrics],
            "root_causes": [bucket.to_json() for bucket in self.root_causes],
            "coverage": [record.to_json() for record in self.coverage],
            "cases": [case.to_json() for case in self.cases],
            "skipped": [dict(item) for item in self.skipped],
        }


@dataclass(frozen=True)
class BaselineMetric:
    library: str
    dimension: str
    preview_before: float
    full_before: float
    acc_b_before: Optional[float] = None
    acc_c_before: Optional[float] = None

    @classmethod
    def from_json(cls, row: JSONDict) -> "BaselineMetric":
        _require_mapping(row, "baseline")
        return cls(
            library=_required_str(row, "library", "baseline"),
            dimension=_required_str(row, "dimension", "baseline"),
            preview_before=_required_float(row, "preview_before", "baseline"),
            full_before=_required_float(row, "full_before", "baseline"),
            acc_b_before=_optional_float(row, "acc_b_before", "baseline"),
            acc_c_before=_optional_float(row, "acc_c_before", "baseline"),
        )

    def to_json(self) -> JSONDict:
        return {
            "library": self.library,
            "dimension": self.dimension,
            "preview_before": self.preview_before,
            "full_before": self.full_before,
            "acc_b_before": self.acc_b_before,
            "acc_c_before": self.acc_c_before,
        }


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    library: str
    dimension: str
    query: str
    question: str
    gold_answers: List[str]
    target_fact_id: Optional[str] = None
    grep_context: List[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, row: JSONDict, *, library: str) -> "EvalCase":
        _require_mapping(row, "case")
        gold = row.get("gold_answers")
        if not isinstance(gold, list) or not gold or not all(isinstance(item, str) and item for item in gold):
            raise RetrievalBenchmarkError("invalid_manifest", "case.gold_answers must be a non-empty list of strings")
        grep_context = row.get("grep_context", [])
        if not isinstance(grep_context, list) or not all(isinstance(item, str) for item in grep_context):
            raise RetrievalBenchmarkError("invalid_manifest", "case.grep_context must be a list of strings")
        target_fact_id = row.get("target_fact_id")
        if target_fact_id is not None and (not isinstance(target_fact_id, str) or not target_fact_id):
            raise RetrievalBenchmarkError(
                "invalid_manifest",
                "case.target_fact_id must be a non-empty string or null",
            )
        return cls(
            case_id=_required_str(row, "case_id", "case"),
            library=library,
            dimension=_required_str(row, "dimension", "case"),
            query=_required_str(row, "query", "case"),
            question=_required_str(row, "question", "case"),
            gold_answers=list(gold),
            target_fact_id=target_fact_id,
            grep_context=list(grep_context),
        )

    def to_json(self, *, include_gold: bool = True) -> JSONDict:
        row: JSONDict = {
            "case_id": self.case_id,
            "library": self.library,
            "dimension": self.dimension,
            "query": self.query,
            "question": self.question,
            "target_fact_id": self.target_fact_id,
            "grep_context": list(self.grep_context),
        }
        if include_gold:
            row["gold_answers"] = list(self.gold_answers)
        return row


@dataclass(frozen=True)
class LibraryPlan:
    name: str
    repo: str
    snapshot_id: str
    cases: List[EvalCase]

    @classmethod
    def from_json(cls, row: JSONDict) -> "LibraryPlan":
        _require_mapping(row, "library")
        name = _required_str(row, "name", "library")
        cases = row.get("cases")
        if not isinstance(cases, list):
            raise RetrievalBenchmarkError("invalid_manifest", "library.cases must be a list")
        return cls(
            name=name,
            repo=_required_str(row, "repo", "library"),
            snapshot_id=_required_str(row, "snapshot_id", "library"),
            cases=[EvalCase.from_json(case, library=name) for case in cases],
        )

    def to_json(self) -> JSONDict:
        return {
            "name": self.name,
            "repo": self.repo,
            "snapshot_id": self.snapshot_id,
            "cases": [case.to_json() for case in self.cases],
        }


@dataclass(frozen=True)
class ModelPlan:
    enabled: bool
    adapter_kind: str
    command: List[str]
    required_env: List[str]
    model_label: str
    timeout_seconds: int
    max_cases: Optional[int] = None

    @classmethod
    def from_json(cls, row: Optional[JSONDict]) -> Optional["ModelPlan"]:
        if row is None:
            return None
        _require_mapping(row, "model_plan")
        command = row.get("command")
        required_env = row.get("required_env", [])
        max_cases = row.get("max_cases")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise RetrievalBenchmarkError("invalid_manifest", "model_plan.command must be a non-empty list of strings")
        if not isinstance(required_env, list) or not all(isinstance(item, str) and item for item in required_env):
            raise RetrievalBenchmarkError("invalid_manifest", "model_plan.required_env must be a list of strings")
        if max_cases is not None and (not isinstance(max_cases, int) or isinstance(max_cases, bool) or max_cases < 1):
            raise RetrievalBenchmarkError(
                "invalid_manifest",
                "model_plan.max_cases must be a positive integer or null",
            )
        timeout = row.get("timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            raise RetrievalBenchmarkError("invalid_manifest", "model_plan.timeout_seconds must be a positive integer")
        enabled = row.get("enabled", False)
        if not isinstance(enabled, bool):
            raise RetrievalBenchmarkError("invalid_manifest", "model_plan.enabled must be a boolean")
        adapter_kind = _required_str(row, "adapter_kind", "model_plan")
        if adapter_kind != "external_command":
            raise RetrievalBenchmarkError("invalid_manifest", "model_plan.adapter_kind must be external_command")
        return cls(
            enabled=enabled,
            adapter_kind=adapter_kind,
            command=list(command),
            required_env=list(required_env),
            model_label=_required_str(row, "model_label", "model_plan"),
            timeout_seconds=timeout,
            max_cases=max_cases,
        )

    def to_json(self) -> JSONDict:
        return {
            "enabled": self.enabled,
            "adapter_kind": self.adapter_kind,
            "command": list(self.command),
            "required_env": list(self.required_env),
            "model_label": self.model_label,
            "timeout_seconds": self.timeout_seconds,
            "max_cases": self.max_cases,
        }


@dataclass(frozen=True)
class RetestManifest:
    libraries: List[LibraryPlan]
    seed: int
    clang16_gold_version: str
    baselines: List[BaselineMetric]
    model_plan: Optional[ModelPlan] = None

    @classmethod
    def from_json(cls, row: JSONDict) -> "RetestManifest":
        _require_mapping(row, "manifest")
        libraries = row.get("libraries")
        baselines = row.get("baselines", [])
        seed = row.get("seed")
        if not isinstance(libraries, list) or not libraries:
            raise RetrievalBenchmarkError("invalid_manifest", "manifest.libraries must be a non-empty list")
        if not isinstance(baselines, list):
            raise RetrievalBenchmarkError("invalid_manifest", "manifest.baselines must be a list")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise RetrievalBenchmarkError("invalid_manifest", "manifest.seed must be an integer")
        return cls(
            libraries=[LibraryPlan.from_json(item) for item in libraries],
            seed=seed,
            clang16_gold_version=_required_str(row, "clang16_gold_version", "manifest"),
            baselines=[BaselineMetric.from_json(item) for item in baselines],
            model_plan=ModelPlan.from_json(row.get("model_plan")),
        )

    def baseline_for(self, library: str, dimension: str) -> Optional[BaselineMetric]:
        for baseline in self.baselines:
            if baseline.library == library and baseline.dimension == dimension:
                return baseline
        return None

    def to_json(self) -> JSONDict:
        return {
            "libraries": [library.to_json() for library in self.libraries],
            "seed": self.seed,
            "clang16_gold_version": self.clang16_gold_version,
            "baselines": [baseline.to_json() for baseline in self.baselines],
            "model_plan": self.model_plan.to_json() if self.model_plan else None,
        }


@dataclass(frozen=True)
class ModelRequest:
    case_id: str
    condition: str
    question: str
    grep_context: List[str]
    cipher_context: Optional[JSONDict] = None

    def to_json(self) -> JSONDict:
        return {
            "case_id": self.case_id,
            "condition": self.condition,
            "question": self.question,
            "grep_context": list(self.grep_context),
            "cipher_context": self.cipher_context,
        }


@dataclass(frozen=True)
class ModelPrediction:
    case_id: str
    condition: str
    answer_names: List[str]
    raw_answer: str
    abstained: bool = False
    error_code: Optional[str] = None

    @classmethod
    def from_json(cls, row: JSONDict) -> "ModelPrediction":
        _require_mapping(row, "prediction")
        answers = row.get("answer_names")
        if not isinstance(answers, list) or not all(isinstance(item, str) for item in answers):
            raise RetrievalBenchmarkError("invalid_adapter_response", "prediction.answer_names must be a list")
        condition = _required_str(row, "condition", "prediction")
        if condition not in {"grep", "grep_cipher"}:
            raise RetrievalBenchmarkError("invalid_adapter_response", "prediction.condition must be grep or grep_cipher")
        error_code = row.get("error_code")
        if error_code is not None and not isinstance(error_code, str):
            raise RetrievalBenchmarkError("invalid_adapter_response", "prediction.error_code must be a string or null")
        return cls(
            case_id=_required_str(row, "case_id", "prediction"),
            condition=condition,
            answer_names=list(answers),
            raw_answer=str(row.get("raw_answer", "")),
            abstained=bool(row.get("abstained", False)),
            error_code=error_code,
        )

    def to_json(self) -> JSONDict:
        return {
            "case_id": self.case_id,
            "condition": self.condition,
            "answer_names": list(self.answer_names),
            "raw_answer": self.raw_answer,
            "abstained": self.abstained,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class RetrievalMetric:
    library: str
    dimension: str
    case_count: int
    recover_preview: float
    recover_full: float
    preview_gap: float
    ceiling_delta: float
    skipped_reason: Optional[str] = None

    def to_json(self) -> JSONDict:
        return {
            "library": self.library,
            "dimension": self.dimension,
            "case_count": self.case_count,
            "recover_preview": self.recover_preview,
            "recover_full": self.recover_full,
            "preview_gap": self.preview_gap,
            "ceiling_delta": self.ceiling_delta,
            "skipped_reason": self.skipped_reason,
        }


@dataclass(frozen=True)
class WeakModelABMetric:
    library: str
    dimension: str
    case_count: int
    acc_b: float
    acc_c: float
    delta: float
    rescue: float
    skipped_reason: Optional[str] = None

    def to_json(self) -> JSONDict:
        return {
            "library": self.library,
            "dimension": self.dimension,
            "case_count": self.case_count,
            "acc_b": self.acc_b,
            "acc_c": self.acc_c,
            "delta": self.delta,
            "rescue": self.rescue,
            "skipped_reason": self.skipped_reason,
        }


@dataclass(frozen=True)
class EvalRunSummary:
    run_id: str
    code_revision_label: str
    libraries: List[str]
    retrieval: List[RetrievalMetric]
    weak_model_ab: List[WeakModelABMetric]
    skipped: List[JSONDict]

    def to_json(self) -> JSONDict:
        return {
            "run_id": self.run_id,
            "code_revision_label": self.code_revision_label,
            "libraries": list(self.libraries),
            "retrieval": [metric.to_json() for metric in self.retrieval],
            "weak_model_ab": [metric.to_json() for metric in self.weak_model_ab],
            "skipped": list(self.skipped),
        }


def _required_str(row: Dict[str, Any], key: str, label: Optional[str] = None) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        field = f"{label}.{key}" if label else key
        raise RetrievalBenchmarkError("invalid_manifest", f"{field} must be a non-empty string")
    return value


def _optional_str(value: Any, key: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RetrievalBenchmarkError("invalid_manifest", f"{key} must be a string or null")
    return value


def _string_list(value: Any, key: str) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RetrievalBenchmarkError("invalid_manifest", f"{key} must be a string list")
    return list(value)


def _resolve_manifest_path(raw_path: str, manifest_dir: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return manifest_dir / path


def unique_sorted(values: Iterable[str]) -> List[str]:
    return sorted({value for value in values if value})


def _require_mapping(row: Any, label: str) -> None:
    if not isinstance(row, dict):
        raise RetrievalBenchmarkError("invalid_manifest", f"{label} must be an object")


def _required_float(row: JSONDict, key: str, label: str) -> float:
    value = row.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RetrievalBenchmarkError("invalid_manifest", f"{label}.{key} must be a number")
    return float(value)


def _optional_float(row: JSONDict, key: str, label: str) -> Optional[float]:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RetrievalBenchmarkError("invalid_manifest", f"{label}.{key} must be a number or null")
    return float(value)

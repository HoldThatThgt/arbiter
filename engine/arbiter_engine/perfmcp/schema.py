from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "perf-mcp.scan.v1"

SEVERITIES = ("low", "medium", "high")
CONFIDENCES = ("low", "medium", "high")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
CONFIDENCE_RANK = {name: index for index, name in enumerate(CONFIDENCES)}


@dataclass(frozen=True)
class Location:
    path: str
    line: int
    column: int = 1
    function: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "line": self.line,
            "column": self.column,
        }
        if self.function:
            data["function"] = self.function
        return data


@dataclass
class Finding:
    rule_id: str
    title: str
    severity: str
    confidence: str
    location: Location
    evidence: dict[str, Any]
    impact: str
    recommendation: str
    tags: list[str] = field(default_factory=list)
    agent_hints: dict[str, str] = field(default_factory=dict)
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "title": self.title,
            "severity": self.severity,
            "confidence": self.confidence,
            "location": self.location.to_dict(),
            "evidence": self.evidence,
            "impact": self.impact,
            "recommendation": self.recommendation,
            "tags": self.tags,
            "agent_hints": self.agent_hints,
        }


@dataclass(frozen=True)
class RuleInfo:
    rule_id: str
    title: str
    default_severity: str
    default_confidence: str
    impact: str
    recommendation: str
    tags: tuple[str, ...]
    why_it_matters: str
    false_positive_checks: tuple[str, ...]
    fix_strategy: tuple[str, ...]
    measurement_plan: tuple[str, ...]

    def to_explanation(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "default_severity": self.default_severity,
            "default_confidence": self.default_confidence,
            "why_it_matters": self.why_it_matters,
            "false_positive_checks": list(self.false_positive_checks),
            "fix_strategy": list(self.fix_strategy),
            "measurement_plan": list(self.measurement_plan),
            "arbiter_contract": {
                "stable_rule_id": self.rule_id,
                "location_required": True,
                "expected_evidence_fields": ["snippet", "loop_start_line"],
            },
        }


RULES: dict[str, RuleInfo] = {
    "C.PERF.STRLEN_IN_LOOP": RuleInfo(
        rule_id="C.PERF.STRLEN_IN_LOOP",
        title="Length calculation inside loop",
        default_severity="medium",
        default_confidence="high",
        impact="Repeated string length scans can turn a linear loop into quadratic work.",
        recommendation="Cache invariant lengths before the loop or carry the length alongside the buffer.",
        tags=("c", "loop", "string", "complexity"),
        why_it_matters=(
            "C string length functions scan until the terminator. Calling them in a hot loop "
            "often repeats the same memory walk and can dominate runtime for long buffers."
        ),
        false_positive_checks=(
            "The string may intentionally change on every iteration.",
            "The loop trip count or string length may be provably tiny.",
            "The compiler may optimize only when it can prove the pointed-to bytes do not change.",
        ),
        fix_strategy=(
            "Move invariant strlen calls before the loop.",
            "If the buffer mutates, update a length variable at the mutation site.",
            "Keep the patch behavior-preserving and add a benchmark around long inputs.",
        ),
        measurement_plan=(
            "Benchmark inputs where the loop count and string length both scale.",
            "Compare wall time and CPU time before and after the length cache.",
        ),
    ),
    "C.PERF.ALLOC_IN_LOOP": RuleInfo(
        rule_id="C.PERF.ALLOC_IN_LOOP",
        title="Heap allocation churn inside loop",
        default_severity="high",
        default_confidence="high",
        impact="Allocator calls in hot loops add synchronization, metadata traffic, fragmentation risk, and cache misses.",
        recommendation="Reuse buffers, preallocate outside the loop, or switch to an arena/scratch allocator.",
        tags=("c", "loop", "allocation", "memory"),
        why_it_matters=(
            "malloc/calloc/realloc/free are much more expensive than stack arithmetic and may serialize "
            "across threads. In tight loops, allocator churn is frequently visible in profiles."
        ),
        false_positive_checks=(
            "The loop may execute rarely on a cold path.",
            "The allocation size may be intentionally unbounded and ownership may escape each iteration.",
            "A custom allocator hidden behind macros may already make the operation cheap.",
        ),
        fix_strategy=(
            "Identify ownership and lifetime first.",
            "Pre-size or reuse storage when each iteration needs temporary memory.",
            "For growing collections, use geometric capacity growth instead of per-item realloc.",
        ),
        measurement_plan=(
            "Run the workload with allocator statistics if available.",
            "Compare allocation count and peak RSS, not only wall time.",
        ),
    ),
    "C.PERF.REALLOC_GROW_ONE": RuleInfo(
        rule_id="C.PERF.REALLOC_GROW_ONE",
        title="Repeated realloc growth pattern",
        default_severity="high",
        default_confidence="high",
        impact="Growing a buffer one element at a time can repeatedly copy all prior data.",
        recommendation="Track capacity separately and grow geometrically, usually doubling until the requested size fits.",
        tags=("c", "loop", "allocation", "complexity"),
        why_it_matters=(
            "realloc may move the allocation and copy the entire old payload. Doing this for every appended "
            "element can make appends quadratic."
        ),
        false_positive_checks=(
            "The allocator may extend in place for this exact workload, but relying on that is fragile.",
            "The buffer may stay very small in all supported inputs.",
        ),
        fix_strategy=(
            "Introduce len/cap fields or local capacity variables.",
            "Grow capacity by a factor, then realloc only when len reaches cap.",
            "Preserve overflow checks around capacity arithmetic.",
        ),
        measurement_plan=(
            "Benchmark append-heavy inputs at several sizes.",
            "Track realloc count and copied bytes if allocator tooling is available.",
        ),
    ),
    "C.PERF.BULK_MEMORY_IN_LOOP": RuleInfo(
        rule_id="C.PERF.BULK_MEMORY_IN_LOOP",
        title="Bulk memory or string copy inside loop",
        default_severity="medium",
        default_confidence="medium",
        impact="Repeated copies or clears inside loops can multiply memory bandwidth and hide algorithmic issues.",
        recommendation="Hoist invariant copies, copy only the changed range, or batch the operation outside the loop.",
        tags=("c", "loop", "memory", "copy"),
        why_it_matters=(
            "memcpy/memmove/memset and string copy calls are fast but still scale with bytes touched. "
            "Nested or repeated use can dominate cache and memory bandwidth."
        ),
        false_positive_checks=(
            "The copied byte count may be tiny or compiler-lowered to a register operation.",
            "The copy may be required because each iteration mutates the full buffer.",
        ),
        fix_strategy=(
            "Inspect whether the source, destination, and size are invariant.",
            "Prefer one bulk operation outside the loop when semantics allow it.",
            "For initialization, consider lazy initialization or a dirty-range strategy.",
        ),
        measurement_plan=(
            "Measure CPU counters or wall time on large buffers.",
            "Check cache-miss or memory-bandwidth counters on platforms that expose them.",
        ),
    ),
    "C.PERF.NESTED_LOOP": RuleInfo(
        rule_id="C.PERF.NESTED_LOOP",
        title="Nested loop hot path",
        default_severity="medium",
        default_confidence="medium",
        impact="Nested loops can be correct, but they deserve attention because cost grows multiplicatively.",
        recommendation="Verify the bounds, look for avoidable repeated work, and consider indexing, hashing, or loop fusion.",
        tags=("c", "loop", "complexity"),
        why_it_matters=(
            "A nested loop is the common syntactic shape of O(n*m) or O(n^2) behavior. The right fix depends "
            "on the data structure and bounds."
        ),
        false_positive_checks=(
            "One bound may be a small constant.",
            "The inner loop may break early in almost all cases.",
            "The algorithm may intentionally be dense matrix or image work where nesting is expected.",
        ),
        fix_strategy=(
            "Make the loop bounds explicit in comments or variable names if they are constrained.",
            "Move invariant computations out of the inner loop.",
            "If searching, build an index once and replace repeated scans.",
        ),
        measurement_plan=(
            "Benchmark scaling by doubling the outer input size.",
            "Record whether runtime scales linearly, quadratically, or worse.",
        ),
    ),
    "C.PERF.IO_IN_LOOP": RuleInfo(
        rule_id="C.PERF.IO_IN_LOOP",
        title="I/O call inside loop",
        default_severity="medium",
        default_confidence="medium",
        impact="Repeated I/O calls can dominate runtime through syscalls, formatting, locking, or buffering behavior.",
        recommendation="Buffer output/input, batch operations, or move diagnostic I/O behind a debug gate.",
        tags=("c", "loop", "io"),
        why_it_matters=(
            "printf/fprintf/read/write and similar calls cross library or kernel boundaries. They can be "
            "orders of magnitude slower than in-memory loop work."
        ),
        false_positive_checks=(
            "The loop may intentionally stream data and already rely on buffering.",
            "The I/O may be test-only or debug-only code compiled out in release builds.",
        ),
        fix_strategy=(
            "Batch small writes into a buffer.",
            "Avoid formatted output per item in hot paths.",
            "Keep observability but gate verbose diagnostics outside release hot paths.",
        ),
        measurement_plan=(
            "Measure with stdout/stderr redirected to a file and to /dev/null.",
            "Compare syscall counts when strace/dtrace is available.",
        ),
    ),
    "C.PERF.EXPENSIVE_MATH_IN_LOOP": RuleInfo(
        rule_id="C.PERF.EXPENSIVE_MATH_IN_LOOP",
        title="Expensive math call inside loop",
        default_severity="low",
        default_confidence="medium",
        impact="Library math calls in hot loops can dominate simple numeric kernels.",
        recommendation="Cache invariant results, use recurrence relations, or use cheaper operations when precision allows.",
        tags=("c", "loop", "math"),
        why_it_matters=(
            "pow, sqrt, trig, and logarithm functions can be expensive relative to integer or simple floating-point "
            "operations. They deserve review in numeric hot paths."
        ),
        false_positive_checks=(
            "The function may be required for correctness and input-dependent every iteration.",
            "The loop may not be hot enough to justify a specialized approximation.",
        ),
        fix_strategy=(
            "Hoist invariant math calls.",
            "Replace pow(x, 2) with x * x when precision and type behavior are acceptable.",
            "Validate numerical differences before landing an optimization.",
        ),
        measurement_plan=(
            "Benchmark with representative compiler flags and math-library settings.",
            "Include correctness tolerances for any approximation.",
        ),
    ),
}


def normalize_severity(value: str | None, default: str = "medium") -> str:
    if value in SEVERITY_RANK:
        return value
    return default


def normalize_confidence(value: str | None, default: str = "medium") -> str:
    if value in CONFIDENCE_RANK:
        return value
    return default

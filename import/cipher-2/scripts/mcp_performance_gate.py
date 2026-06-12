from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, Iterator, List

from cipher2.mcp import BUDGETS, open_mcp_server
from cipher2.storage import FactRecord, FactRelative, open_fact_store


WORKLOADS = [
    {"name": "small", "facts": 1_000, "calls": 100, "memory_mb": 16, "timeout_seconds": 5},
    {"name": "medium", "facts": 100_000, "calls": 200, "memory_mb": 128, "timeout_seconds": 120},
    {"name": "large", "facts": 1_000_000, "calls": 20, "memory_mb": 512, "timeout_seconds": 900},
]


def main() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(_facts(int(workload["facts"])))
            server = open_mcp_server(target)

            tracemalloc.start()
            started = time.perf_counter()
            try:
                for index in range(int(workload["calls"])):
                    search = server.search("alpha", limit=5)
                    if search.result_count != 5:
                        raise AssertionError(f"{workload['name']} search result count mismatch")
                    if index % 2 == 0:
                        detail = server.detail(search.results[index % len(search.results)].object_id, budget="small")
                        if detail.fact.object_id != search.results[index % len(search.results)].object_id:
                            raise AssertionError(f"{workload['name']} detail fact mismatch")
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            elapsed = time.perf_counter() - started
            peak_mb = peak / 1024 / 1024
            if peak_mb >= workload["memory_mb"]:
                raise AssertionError(f"{workload['name']} peak memory {peak_mb:.2f}MB exceeds budget")
            if elapsed >= workload["timeout_seconds"]:
                raise AssertionError(f"{workload['name']} elapsed {elapsed:.2f}s exceeds timeout")
            results.append(
                {
                    "workload": workload["name"],
                    "facts": workload["facts"],
                    "calls": workload["calls"],
                    "peak_mb": round(peak_mb, 3),
                    "elapsed_seconds": round(elapsed, 3),
                }
            )
    _run_high_fan_in_detail_budget_gate(results)

    print(json.dumps({"mcp_performance_gate": results}, ensure_ascii=False, sort_keys=True))


def _facts(count: int) -> Iterator[FactRecord]:
    for index in range(count):
        yield FactRecord(
            object_id=f"fact:{index:07d}",
            object_name=f"Function {index}",
            object_description=f"Searchable alpha beta {index}",
            object_source=f"src/module{index % 17}.c:{index + 1}",
            object_profile="debug" if index % 2 else "release",
            object_caller=f"caller:{index % 101}" if index % 3 == 0 else None,
            object_callee=f"callee:{index % 103}" if index % 5 == 0 else None,
            payload={"fact_kind": "function", "rank": index, "body": "x" * 64},
        )


def _run_high_fan_in_detail_budget_gate(results: List[Dict[str, float]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
            _high_fan_in_facts(),
            _high_fan_in_relatives(),
        )
        server = open_mcp_server(target)

        tracemalloc.start()
        started = time.perf_counter()
        try:
            detail = server.detail("fact:high_fan_in", budget="large")
            body = json.dumps(detail.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        elapsed = time.perf_counter() - started

    limit = BUDGETS["large"]["response_bytes"]
    if len(body) > limit:
        raise AssertionError(f"high_fan_in detail response {len(body)} bytes exceeds {limit}")
    if not detail.response_truncated or detail.relative_preview.budget_exhausted_kind != "response_bytes":
        raise AssertionError("high_fan_in detail did not report response_bytes truncation")
    results.append(
        {
            "workload": "high_fan_in_detail_large",
            "facts": 1 + (len(_HIGH_FAN_IN_RELATION_KINDS) * 2 * _HIGH_FAN_IN_RELATIVES_PER_BUCKET),
            "calls": 1,
            "peak_mb": round(peak / 1024 / 1024, 3),
            "elapsed_seconds": round(elapsed, 3),
            "response_bytes": len(body),
            "response_bytes_limit": limit,
        }
    )


_HIGH_FAN_IN_RELATION_KINDS = [
    "direct_call",
    "field_read",
    "field_write",
    "has_field",
    "assigned_to",
    "dispatches_via",
    "include",
    "defines",
    "declares",
]
_HIGH_FAN_IN_RELATIVES_PER_BUCKET = 70


def _high_fan_in_facts() -> List[FactRecord]:
    facts = [
        FactRecord(
            object_id="fact:high_fan_in",
            object_name="high_fan_in",
            object_description="high fan in detail budget target",
            object_source="src/high_fan_in.c:42",
            object_profile="debug",
            payload={"fact_kind": "function", **{f"payload_{index:02d}": "value" * 4 for index in range(32)}},
        )
    ]
    for relation_kind in _HIGH_FAN_IN_RELATION_KINDS:
        for direction in ("incoming", "outgoing"):
            for index in range(_HIGH_FAN_IN_RELATIVES_PER_BUCKET):
                facts.append(
                    FactRecord(
                        object_id=f"fact:{direction}:{relation_kind}:{index:02d}",
                        object_name=f"{direction}_{relation_kind}_{index:02d}",
                        object_description="endpoint " + ("x" * 96),
                        object_source=f"src/{relation_kind}_{direction}.c:{index + 1}",
                        object_profile="debug",
                        payload={"fact_kind": "function", "note": "endpoint" * 12},
                    )
                )
    return facts


def _high_fan_in_relatives() -> List[FactRelative]:
    relatives: List[FactRelative] = []
    for relation_kind in _HIGH_FAN_IN_RELATION_KINDS:
        for direction in ("incoming", "outgoing"):
            for index in range(_HIGH_FAN_IN_RELATIVES_PER_BUCKET):
                endpoint_id = f"fact:{direction}:{relation_kind}:{index:02d}"
                from_id = endpoint_id if direction == "incoming" else "fact:high_fan_in"
                to_id = "fact:high_fan_in" if direction == "incoming" else endpoint_id
                relatives.append(
                    FactRelative(
                        relative_id=f"rel:{direction}:{relation_kind}:{index:02d}",
                        from_fact_id=from_id,
                        to_fact_id=to_id,
                        relation_kind=relation_kind,
                        condition=None,
                        object_profile="debug",
                        evidence_source=f"src/{relation_kind}_{direction}.c:{index + 100}",
                        confidence=1.0,
                        payload={"note": "relative" * 12, "line": index},
                    )
                )
    return relatives


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, Iterator, List

from cipher2.mcp import open_mcp_server
from cipher2.storage import FactRecord, FactRelative, RelativeCondition, open_fact_store


WORKLOADS = [
    {"name": "small", "facts": 1_000, "relatives": 2_000, "calls": 100, "memory_mb": 16, "timeout_seconds": 10},
    {"name": "medium", "facts": 100_000, "relatives": 200_000, "calls": 200, "memory_mb": 128, "timeout_seconds": 180},
    {"name": "large", "facts": 1_000_000, "relatives": 2_000_000, "calls": 20, "memory_mb": 512, "timeout_seconds": 1_200},
]


def main() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            fact_count = int(workload["facts"])
            relative_count = int(workload["relatives"])
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                _facts(fact_count),
                _relatives(fact_count, relative_count),
            )
            server = open_mcp_server(target)

            tracemalloc.start()
            started = time.perf_counter()
            try:
                for index in range(int(workload["calls"])):
                    fact_id = f"fact:{index % fact_count:07d}"
                    detail = server.detail(fact_id, budget="small")
                    if detail.fact.object_id != fact_id:
                        raise AssertionError(f"{workload['name']} detail fact mismatch")
                    if not detail.relative_preview.relatives:
                        raise AssertionError(f"{workload['name']} relative preview is empty")
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
                    "facts": fact_count,
                    "relatives": relative_count,
                    "calls": workload["calls"],
                    "peak_mb": round(peak_mb, 3),
                    "elapsed_seconds": round(elapsed, 3),
                }
            )

    print(json.dumps({"mcp_relative_preview_performance_gate": results}, ensure_ascii=False, sort_keys=True))


def _facts(count: int) -> Iterator[FactRecord]:
    for index in range(count):
        yield FactRecord(
            object_id=f"fact:{index:07d}",
            object_name=f"Function {index}",
            object_description=f"relative endpoint {index}",
            object_source=f"src/module{index % 17}.c:{index + 1}",
            object_profile="default",
            payload={"fact_kind": "function", "body": "x" * 32},
        )


def _relatives(fact_count: int, relative_count: int) -> Iterator[FactRelative]:
    for index in range(relative_count):
        condition = None
        if index % 10 == 0:
            condition = RelativeCondition(kind="branch", branch="then", source=f"src/module{index % 17}.c:{index + 1}")
        yield FactRelative(
            relative_id=f"rel:{index:07d}",
            from_fact_id=f"fact:{index % fact_count:07d}",
            to_fact_id=f"fact:{(index + 1) % fact_count:07d}",
            relation_kind="direct_call",
            condition=condition,
            object_profile="default",
            evidence_source=f"src/module{index % 17}.c:{index + 1}",
            confidence=1.0,
            payload={"rank": index},
        )


if __name__ == "__main__":
    main()

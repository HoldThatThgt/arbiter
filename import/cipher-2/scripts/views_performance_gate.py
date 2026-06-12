from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, Iterator, List

from cipher2.storage import FactRecord, open_fact_store
from cipher2.tools.views import build_overview


WORKLOADS = [
    {"name": "small", "facts": 1_000, "events": 1_000, "memory_mb": 5, "timeout_seconds": 2},
    {"name": "medium", "facts": 100_000, "events": 100_000, "memory_mb": 40, "timeout_seconds": 20},
    {"name": "large", "facts": 1_000_000, "events": 1_000_000, "memory_mb": 80, "timeout_seconds": 60},
]


def main() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(_facts(int(workload["facts"])))
            _write_log_fixture(target, int(workload["events"]))

            tracemalloc.start()
            started = time.perf_counter()
            try:
                overview = build_overview(target, top_n=10)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            elapsed = time.perf_counter() - started

            if overview.storage is None or overview.storage.total_facts != workload["facts"]:
                raise AssertionError(f"{workload['name']} storage view mismatch")
            if overview.log is None or overview.log.total_events != workload["events"]:
                raise AssertionError(f"{workload['name']} log view mismatch")
            peak_mb = peak / 1024 / 1024
            if peak_mb >= workload["memory_mb"]:
                raise AssertionError(f"{workload['name']} peak memory {peak_mb:.2f}MB exceeds budget")
            if elapsed >= workload["timeout_seconds"]:
                raise AssertionError(f"{workload['name']} elapsed {elapsed:.2f}s exceeds timeout")
            results.append(
                {
                    "workload": workload["name"],
                    "facts": workload["facts"],
                    "events": workload["events"],
                    "peak_mb": round(peak_mb, 3),
                    "elapsed_seconds": round(elapsed, 3),
                }
            )

    print(json.dumps({"views_performance_gate": results}, ensure_ascii=False, sort_keys=True))


def _facts(count: int) -> Iterator[FactRecord]:
    for index in range(count):
        yield FactRecord(
            object_id=f"fact:{index:07d}",
            object_name=f"Fact {index}",
            object_description=f"Views performance fact {index}",
            object_source=f"src/module{index % 17}.py:{index}",
            object_profile="debug" if index % 2 else "release",
            object_caller=f"caller:{index % 101}" if index % 3 == 0 else None,
            object_callee=f"callee:{index % 103}" if index % 5 == 0 else None,
            payload={"fact_kind": "function" if index % 2 else "doc", "rank": index},
        )


def _write_log_fixture(target: Path, events: int) -> None:
    path = target / ".cipher" / "log" / "mcp.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index in range(events):
            row = {
                "channel": "mcp",
                "correlation_id": None,
                "counts": {"matched_count": index % 7},
                "duration_ms": float(index % 19),
                "error_code": None,
                "event_name": "mcp.search",
                "payload": {"query_kind": "substring", "limit": 20},
                "schema_version": 1,
                "status": "ok",
                "subject_id": None,
                "summary": None,
                "timestamp": "2026-05-25T10:00:00.000000Z",
            }
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()

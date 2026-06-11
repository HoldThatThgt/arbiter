from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, List

from cipher2.tools.log import LogEvent, open_log


WORKLOADS = [
    {"name": "small", "events": 1_000, "memory_mb": 5, "timeout_seconds": 10},
    {"name": "medium", "events": 100_000, "memory_mb": 40, "timeout_seconds": 60},
    {"name": "large", "events": 1_000_000, "memory_mb": 80, "timeout_seconds": 300},
]


def main() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)
            if workload["name"] == "small":
                _write_small_with_api(log, int(workload["events"]))
            else:
                _write_summary_fixture(target, int(workload["events"]))

            tracemalloc.start()
            started = time.perf_counter()
            try:
                summary = log.summarize(channel="storage")
                limited = log.read_events(channel="storage", limit=5)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            elapsed = time.perf_counter() - started

            if summary.total_events != workload["events"]:
                raise AssertionError(f"{workload['name']} summary count mismatch")
            if len(limited.events) != 5:
                raise AssertionError(f"{workload['name']} limit read mismatch")
            peak_mb = peak / 1024 / 1024
            if peak_mb >= workload["memory_mb"]:
                raise AssertionError(f"{workload['name']} peak memory {peak_mb:.2f}MB exceeds budget")
            if elapsed >= workload["timeout_seconds"]:
                raise AssertionError(f"{workload['name']} elapsed {elapsed:.2f}s exceeds timeout")
            results.append(
                {
                    "workload": workload["name"],
                    "events": workload["events"],
                    "peak_mb": round(peak_mb, 3),
                    "elapsed_seconds": round(elapsed, 3),
                }
            )

    print(json.dumps({"log_performance_gate": results}, ensure_ascii=False, sort_keys=True))


def _write_small_with_api(log, events: int) -> None:
    for index in range(events):
        log.write_event(
            LogEvent(
                event_name="storage.search",
                channel="storage",
                duration_ms=float(index % 17),
                counts={"matched_count": index % 5},
                payload={"query_kind": "substring", "limit": 20},
            )
        )


def _write_summary_fixture(target: Path, events: int) -> None:
    path = target / ".cipher" / "log" / "storage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index in range(events):
            row = {
                "channel": "storage",
                "correlation_id": None,
                "counts": {"matched_count": index % 5},
                "duration_ms": float(index % 17),
                "error_code": None,
                "event_name": "storage.search",
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

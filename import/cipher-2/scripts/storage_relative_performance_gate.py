from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, Iterator, List

from cipher2.storage import FactRecord, FactRelative, RelativeCondition, open_fact_store


WORKLOADS = [
    {"name": "small", "facts": 1_000, "relatives": 2_000, "memory_mb": 16, "timeout_seconds": 20},
    {"name": "medium", "facts": 100_000, "relatives": 200_000, "memory_mb": 160, "timeout_seconds": 180},
    {"name": "large", "facts": 1_000_000, "relatives": 2_000_000, "memory_mb": 768, "timeout_seconds": 1_200},
]


def main() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)
            fact_count = int(workload["facts"])
            relative_count = int(workload["relatives"])
            tracemalloc.start()
            started = time.perf_counter()
            try:
                manifest = store.replace_snapshot(_facts(fact_count), _relatives(fact_count, relative_count))
                reader = open_fact_store(target, mode="r", log_enabled=False)
                index_started = time.perf_counter()
                first_outgoing = reader.relatives_for_fact("fact:0000000", direction="outgoing", limit=20)
                cold_index_ms = (time.perf_counter() - index_started) * 1000
                outgoing = store.relatives_for_fact("fact:0000000", direction="outgoing", limit=20)
                stats = store.stats()
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            elapsed = time.perf_counter() - started

            if manifest.fact_count != fact_count or manifest.relative_count != relative_count:
                raise AssertionError(f"{workload['name']} manifest count mismatch")
            if stats.total_relatives != relative_count:
                raise AssertionError(f"{workload['name']} relative stats mismatch")
            if not first_outgoing or not outgoing:
                raise AssertionError(f"{workload['name']} relations query returned no edges")
            peak_mb = peak / 1024 / 1024
            if peak_mb >= workload["memory_mb"]:
                raise AssertionError(f"{workload['name']} peak memory {peak_mb:.2f}MB exceeds budget")
            if elapsed >= workload["timeout_seconds"]:
                raise AssertionError(f"{workload['name']} elapsed {elapsed:.2f}s exceeds timeout")
            if stats.compressed_data_bytes > stats.uncompressed_bytes * 0.55:
                raise AssertionError(f"{workload['name']} compressed snapshot exceeds 55% of raw logical bytes")
            if stats.bytes_on_disk > stats.uncompressed_bytes * 0.6:
                raise AssertionError(f"{workload['name']} snapshot with read index exceeds 60% of raw logical bytes")
            if stats.compressed_data_bytes and stats.read_index_bytes > stats.compressed_data_bytes * 2:
                raise AssertionError(f"{workload['name']} read index exceeds 2x compressed data bytes")
            results.append(
                {
                    "workload": workload["name"],
                    "facts": fact_count,
                    "relatives": relative_count,
                    "peak_mb": round(peak_mb, 3),
                    "elapsed_seconds": round(elapsed, 3),
                    "raw_snapshot_mb": round(stats.uncompressed_bytes / 1024 / 1024, 3),
                    "compressed_snapshot_mb": round(stats.compressed_data_bytes / 1024 / 1024, 3),
                    "total_snapshot_mb": round(stats.bytes_on_disk / 1024 / 1024, 3),
                    "read_index_mb": round(stats.read_index_bytes / 1024 / 1024, 3),
                    "read_index_to_compressed_ratio": round(
                        stats.read_index_bytes / stats.compressed_data_bytes,
                        3,
                    )
                    if stats.compressed_data_bytes
                    else 0.0,
                    "compression_ratio": stats.compression_ratio,
                    "storage_overhead_ratio": stats.storage_overhead_ratio,
                    "cold_index_open_ms": round(cold_index_ms, 3),
                    "first_search_ms": round(cold_index_ms, 3),
                }
            )

    print(json.dumps({"storage_relative_performance_gate": results}, ensure_ascii=False, sort_keys=True))


def _facts(count: int) -> Iterator[FactRecord]:
    for index in range(count):
        yield FactRecord(
            object_id=f"fact:{index:07d}",
            object_name=f"Function {index}",
            object_description=f"relative endpoint {index}",
            object_source=f"src/module{index % 17}.c:{index + 1}",
            object_profile="default",
            payload={"fact_kind": "function"},
        )


def _relatives(fact_count: int, relative_count: int) -> Iterator[FactRelative]:
    kinds = ("direct_call", "assigned_to", "dispatches_via")
    for index in range(relative_count):
        kind = kinds[index % len(kinds)]
        condition = None
        if index % 10 == 0:
            condition = RelativeCondition(kind="branch", branch="then", source=f"src/module{index % 17}.c:{index + 1}")
        yield FactRelative(
            relative_id=f"rel:{index:07d}",
            from_fact_id=f"fact:{index % fact_count:07d}",
            to_fact_id=f"fact:{(index + 1) % fact_count:07d}",
            relation_kind=kind,
            condition=condition,
            object_profile="default",
            evidence_source=f"src/module{index % 17}.c:{index + 1}",
            confidence=1.0,
            payload={"rank": index},
        )


if __name__ == "__main__":
    main()

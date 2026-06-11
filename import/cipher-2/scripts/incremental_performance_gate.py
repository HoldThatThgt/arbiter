from __future__ import annotations

import hashlib
import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from statistics import quantiles
from typing import Dict, Iterator, List

from cipher2.config import load_config
from cipher2.incremental import DirtySource, IncrementalBuildResult, IncrementalCoordinator
from cipher2.initializer.extractor.code import CodeFactExtractor
from cipher2.storage import FactRecord, FactRelative, SourceInventoryEntry, TemporaryOverlay, open_fact_store


WORKLOADS = [
    {
        "name": "small",
        "facts": 1_000,
        "relatives": 1_000,
        "dirty_sources": 1,
        "memory_mb": 64,
        "publish_p95_ms": 10,
        "query_p95_ms": 20,
    },
    {
        "name": "medium",
        "facts": 100_000,
        "relatives": 100_000,
        "dirty_sources": 100,
        "memory_mb": 256,
        "publish_p95_ms": 20,
        "query_p95_ms": 50,
    },
    {
        "name": "large",
        "facts": 1_000_000,
        "relatives": 1_000_000,
        "dirty_sources": 10_000,
        "memory_mb": 1024,
        "publish_p95_ms": None,
        "query_p95_ms": None,
    },
]


def main() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            dirty_sources = int(workload["dirty_sources"])
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot(
                _facts(int(workload["facts"])),
                _relatives(int(workload["facts"]), int(workload["relatives"])),
                _source_inventory(dirty_sources),
            )
            store.search("", 1)
            config = load_config(
                target,
                overrides={"incremental": {"max_dirty_files": max(500, dirty_sources)}},
                observe=False,
            )
            coordinator = IncrementalCoordinator(target, config, log_enabled=False)
            inventory = list(store.iter_source_inventory())
            header = next(item for item in inventory if item.source_kind == "header")

            started = time.perf_counter()
            planned = coordinator._plan_dirty_sources(header, inventory, _hash_text("changed"))  # noqa: SLF001
            if len(planned) != dirty_sources:
                raise AssertionError(f"{workload['name']} dirty source count mismatch")
            source_ids = {item.source_id for item in planned}

            tracemalloc.start()
            try:
                overlay = TemporaryOverlay(
                    overlay_id=f"overlay-{workload['name']}-memory",
                    view_state="overlay",
                    fact_upserts=list(_overlay_facts(planned, 0)),
                    relative_upserts=[],
                    source_tombstones=source_ids,
                )
                view = store.open_view(overlay)
                visible = view.search("", 5)
                if not visible or not visible[0].object_id.startswith("000-overlay:"):
                    raise AssertionError(f"{workload['name']} overlay query visibility mismatch")
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            publish_samples: List[float] = []
            query_samples: List[float] = []
            for iteration in range(20):
                publish_started = time.perf_counter()
                overlay = TemporaryOverlay(
                    overlay_id=f"overlay-{workload['name']}-{iteration}",
                    view_state="overlay",
                    fact_upserts=list(_overlay_facts(planned, iteration)),
                    relative_upserts=[],
                    source_tombstones=source_ids,
                )
                view = store.open_view(overlay)
                publish_samples.append((time.perf_counter() - publish_started) * 1000)

                query_started = time.perf_counter()
                visible = view.search("", 5)
                query_samples.append((time.perf_counter() - query_started) * 1000)
                if not visible or not visible[0].object_id.startswith("000-overlay:"):
                    raise AssertionError(f"{workload['name']} overlay query visibility mismatch")
            elapsed = time.perf_counter() - started

            peak_mb = peak / 1024 / 1024
            publish_p95 = _p95(publish_samples)
            query_p95 = _p95(query_samples)
            if peak_mb >= workload["memory_mb"]:
                raise AssertionError(f"{workload['name']} peak memory {peak_mb:.2f}MB exceeds budget")
            if workload["publish_p95_ms"] is not None and publish_p95 >= workload["publish_p95_ms"]:
                raise AssertionError(f"{workload['name']} publish p95 {publish_p95:.2f}ms exceeds budget")
            if workload["query_p95_ms"] is not None and query_p95 >= workload["query_p95_ms"]:
                raise AssertionError(f"{workload['name']} query p95 {query_p95:.2f}ms exceeds budget")
            results.append(
                {
                    "workload": workload["name"],
                    "facts": workload["facts"],
                    "relatives": workload["relatives"],
                    "dirty_sources": dirty_sources,
                    "peak_mb": round(peak_mb, 3),
                    "publish_p95_ms": round(publish_p95, 3),
                    "query_p95_ms": round(query_p95, 3),
                    "elapsed_seconds": round(elapsed, 3),
                }
            )

    results.append(_guard_hot_path_result())
    print(json.dumps({"incremental_performance_gate": results}, ensure_ascii=False, sort_keys=True))


def _guard_hot_path_result() -> Dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        (target / "bin").mkdir(parents=True)
        clang = target / "bin" / "clang"
        clang.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        clang.chmod(0o755)
        source = SourceInventoryEntry(
            source_id="source:guard",
            rel_path="src/guard.c",
            source_kind="c_source",
            sha256=_hash_text("base"),
            size_bytes=4,
            mtime_ns=1,
            compile_command_hash=None,
            toolchain_hash=_hash_text("base-toolchain"),
            included_by=[],
            includes=[],
        )
        store = open_fact_store(target, mode="w", log_enabled=False)
        store.replace_snapshot(
            [
                FactRecord(
                    object_id="fact:guard-base",
                    object_name="GuardBase",
                    object_description="base guard fact",
                    object_source="src/guard.c:1",
                    object_profile="default",
                    payload={"fact_kind": "function", "source_id": source.source_id},
                )
            ],
            [],
            [source],
        )
        config = load_config(
            target,
            overrides={"extractor": {"code": {"clang_executable": "bin/clang"}}},
            observe=False,
        )
        coordinator = IncrementalCoordinator(target, config, log_enabled=False)
        dirty = [
            DirtySource(
                source_id=source.source_id,
                rel_path=source.rel_path,
                reason="content_changed",
                previous_sha256=source.sha256,
                current_sha256=_hash_text("overlay"),
            )
        ]
        result = IncrementalBuildResult(
            facts=[
                FactRecord(
                    object_id="fact:guard-overlay",
                    object_name="GuardOverlay",
                    object_description="overlay guard fact",
                    object_source="src/guard.c:1",
                    object_profile="default",
                    payload={"fact_kind": "function", "source_id": source.source_id},
                )
            ],
            relatives=[],
            source_inventory=[],
        )
        probe_count = 0
        original_validate = CodeFactExtractor._validate_toolchain

        def counting_validate(extractor):
            nonlocal probe_count
            probe_count += 1
            extractor.toolchain_probe_result = None

        CodeFactExtractor._validate_toolchain = counting_validate
        try:
            read_store = open_fact_store(target, mode="r", log_enabled=False)
            coordinator._publish_overlay(read_store, read_store.stats().snapshot_id, dirty, result, time.perf_counter())  # noqa: SLF001
            if probe_count != 1:
                raise AssertionError(f"guard hot path expected one publish-time probe, got {probe_count}")
            query_samples: List[float] = []
            for _iteration in range(50):
                started = time.perf_counter()
                view = coordinator.current_view()
                visible = view.search("GuardOverlay", 1)
                query_samples.append((time.perf_counter() - started) * 1000)
                if view.view_state != "overlay" or not visible:
                    raise AssertionError("guard hot path overlay query visibility mismatch")
            if probe_count != 1:
                raise AssertionError(f"guard hot path reprobed toolchain on query: {probe_count}")
        finally:
            CodeFactExtractor._validate_toolchain = original_validate
        query_p95 = _p95(query_samples)
        if query_p95 >= 20:
            raise AssertionError(f"guard hot path query p95 {query_p95:.2f}ms exceeds budget")
        return {
            "workload": "guard_hot_path",
            "facts": 1,
            "relatives": 0,
            "dirty_sources": 1,
            "peak_mb": 0.0,
            "publish_p95_ms": 0.0,
            "query_p95_ms": round(query_p95, 3),
            "elapsed_seconds": 0.0,
            "toolchain_probe_count": probe_count,
        }


def _facts(count: int) -> Iterator[FactRecord]:
    for index in range(count):
        source_index = index % 10_000
        yield FactRecord(
            object_id=f"fact:{index:07d}",
            object_name=f"Function {index}",
            object_description=f"base function {index}",
            object_source=f"src/unit_{source_index:05d}.c:{index + 1}",
            object_profile="default",
            payload={"fact_kind": "function", "source_id": f"source:{source_index:05d}"},
        )


def _relatives(fact_count: int, relative_count: int) -> Iterator[FactRelative]:
    for index in range(relative_count):
        yield FactRelative(
            relative_id=f"rel:{index:07d}",
            from_fact_id=f"fact:{index % fact_count:07d}",
            to_fact_id=f"fact:{(index + 1) % fact_count:07d}",
            relation_kind="direct_call",
            condition=None,
            object_profile="default",
            evidence_source=f"src/unit_{index % 10_000:05d}.c:{index + 1}",
            confidence=1.0,
            payload={"source_id": f"source:{index % 10_000:05d}"},
        )


def _source_inventory(dirty_sources: int) -> Iterator[SourceInventoryEntry]:
    header_id = "source:header"
    yield SourceInventoryEntry(
        source_id=header_id,
        rel_path="include/common.h",
        source_kind="header",
        sha256=_hash_text("header"),
        size_bytes=6,
        mtime_ns=1,
        compile_command_hash=None,
        toolchain_hash=_hash_text("toolchain"),
        included_by=[f"source:{index:05d}" for index in range(dirty_sources)],
        includes=[],
    )
    for index in range(dirty_sources):
        yield SourceInventoryEntry(
            source_id=f"source:{index:05d}",
            rel_path=f"src/unit_{index:05d}.c",
            source_kind="c_source",
            sha256=_hash_text(f"source-{index}"),
            size_bytes=32,
            mtime_ns=1,
            compile_command_hash=_hash_text(f"compile-{index}"),
            toolchain_hash=_hash_text("toolchain"),
            included_by=[],
            includes=[header_id],
        )


def _overlay_facts(dirty_sources, generation: int) -> Iterator[FactRecord]:
    for index, source in enumerate(dirty_sources):
        yield FactRecord(
            object_id=f"000-overlay:{generation:02d}:{index:05d}",
            object_name=f"Overlay {index}",
            object_description=f"incremental overlay fact {index}",
            object_source=f"{source.rel_path}:1",
            object_profile="default",
            payload={"fact_kind": "function", "source_id": source.source_id},
        )


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _p95(values: List[float]) -> float:
    if len(values) < 2:
        return values[0]
    return quantiles(values, n=20)[18]


if __name__ == "__main__":
    main()

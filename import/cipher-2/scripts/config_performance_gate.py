from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, List

from cipher2.config import load_config, write_default_config


WORKLOADS = [
    {"name": "small", "loads": 1_000, "memory_mb": 5, "timeout_seconds": 2},
    {"name": "medium", "loads": 100_000, "memory_mb": 40, "timeout_seconds": 30},
    {"name": "large", "loads": 1_000_000, "memory_mb": 80, "timeout_seconds": 300},
]


def main() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            compile_db.parent.mkdir()
            compile_db.write_text("[]", encoding="utf-8")
            write_default_config(target, compile_database="build/compile_commands.json", observe=False)
            expected_compile_db = compile_db.resolve(strict=False)

            tracemalloc.start()
            started = time.perf_counter()
            try:
                for _index in range(int(workload["loads"])):
                    config = load_config(target, observe=False)
                    if config.compile_database_path != expected_compile_db:
                        raise AssertionError(f"{workload['name']} compile database mismatch")
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
                    "loads": workload["loads"],
                    "peak_mb": round(peak_mb, 3),
                    "elapsed_seconds": round(elapsed, 3),
                }
            )

    print(json.dumps({"config_performance_gate": results}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

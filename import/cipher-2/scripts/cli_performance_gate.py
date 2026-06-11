from __future__ import annotations

import io
import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from cipher2.cli import main
from cipher2.config import write_default_config


WORKLOADS = [
    {
        "name": "small",
        "loc": 1_000,
        "files": 10,
        "memory_mb": 80,
        "timeout_seconds": 8,
        "status_memory_mb": 64,
        "status_timeout_seconds": 1,
    },
    {
        "name": "medium",
        "loc": 100_000,
        "files": 1_000,
        "memory_mb": 640,
        "timeout_seconds": 150,
        "status_memory_mb": 256,
        "status_timeout_seconds": 10,
    },
    {
        "name": "large",
        "loc": 1_000_000,
        "files": 10_000,
        "memory_mb": 2_560,
        "timeout_seconds": 1_000,
        "status_memory_mb": 512,
        "status_timeout_seconds": 90,
    },
]


def main_gate() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_fixture(target, int(workload["loc"]), int(workload["files"]))
            _write_fake_toolchain(target)
            stdout = io.StringIO()
            stderr = io.StringIO()

            tracemalloc.start()
            started = time.perf_counter()
            try:
                exit_code = main(["init", str(target), "--json"], stdout=stdout, stderr=stderr)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            elapsed = time.perf_counter() - started

            if exit_code != 0:
                raise AssertionError(f"{workload['name']} cli init failed: {stderr.getvalue()}")
            summary = json.loads(stdout.getvalue())
            if not summary["ok"]:
                raise AssertionError(f"{workload['name']} cli summary is not ok")
            if summary["source_count"] != workload["files"]:
                raise AssertionError(f"{workload['name']} source count mismatch")
            peak_mb = peak / 1024 / 1024
            if peak_mb >= workload["memory_mb"]:
                raise AssertionError(f"{workload['name']} peak memory {peak_mb:.2f}MB exceeds budget")
            if elapsed >= workload["timeout_seconds"]:
                raise AssertionError(f"{workload['name']} elapsed {elapsed:.2f}s exceeds timeout")

            human_elapsed, human_peak_mb, human_stdout = _measure_cli(["status", str(target)])
            json_elapsed, json_peak_mb, json_stdout = _measure_cli(["status", str(target), "--json"])
            status_memory_mb = float(workload["status_memory_mb"])
            status_timeout_seconds = float(workload["status_timeout_seconds"])
            if "storage:" not in human_stdout:
                raise AssertionError(f"{workload['name']} status human output missing storage section")
            status_payload = json.loads(json_stdout)
            if sorted(status_payload) != ["errors", "generated_at", "incremental", "log", "state", "storage"]:
                raise AssertionError(f"{workload['name']} status JSON is not full overview")
            if human_peak_mb >= status_memory_mb:
                raise AssertionError(f"{workload['name']} status human peak {human_peak_mb:.2f}MB exceeds budget")
            if json_peak_mb >= status_memory_mb:
                raise AssertionError(f"{workload['name']} status JSON peak {json_peak_mb:.2f}MB exceeds budget")
            if human_elapsed >= status_timeout_seconds:
                raise AssertionError(f"{workload['name']} status human elapsed {human_elapsed:.2f}s exceeds timeout")
            if json_elapsed >= status_timeout_seconds:
                raise AssertionError(f"{workload['name']} status JSON elapsed {json_elapsed:.2f}s exceeds timeout")

            results.append(
                {
                    "workload": workload["name"],
                    "loc": workload["loc"],
                    "files": workload["files"],
                    "facts": summary["fact_count"],
                    "peak_mb": round(peak_mb, 3),
                    "elapsed_seconds": round(elapsed, 3),
                    "status_human_peak_mb": round(human_peak_mb, 3),
                    "status_human_seconds": round(human_elapsed, 3),
                    "status_json_peak_mb": round(json_peak_mb, 3),
                    "status_json_seconds": round(json_elapsed, 3),
                }
            )

    print(json.dumps({"cli_performance_gate": results}, ensure_ascii=False, sort_keys=True))


def _measure_cli(argv: Sequence[str]) -> Tuple[float, float, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        exit_code = main(argv, stdout=stdout, stderr=stderr)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    elapsed = time.perf_counter() - started
    if exit_code != 0:
        raise AssertionError(f"{' '.join(argv)} failed: {stderr.getvalue()}")
    return elapsed, peak / 1024 / 1024, stdout.getvalue()


def _write_fixture(target: Path, loc: int, files: int) -> None:
    source_dir = target / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    loc_per_file = max(1, loc // files)
    for index in range(files):
        lines = [
            f"#define LIMIT_{index} {index}\n",
            f"int global_{index} = {index};\n",
            f"int func_{index}(void) {{ return global_{index}; }}\n",
        ]
        remaining = max(0, loc_per_file - len(lines))
        for line in range(remaining):
            lines.append(f"/* filler {index}:{line} */\n")
        path = source_dir / f"unit_{index:05d}.c"
        path.write_text("".join(lines), encoding="utf-8")


def _write_fake_toolchain(target: Path) -> None:
    clang = target / "bin" / "clang"
    gcc = target / "bin" / "gcc"
    clang.parent.mkdir(parents=True, exist_ok=True)
    clang.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'clang version 16.0.6'; exit 0; fi\n"
        "echo '{\"kind\":\"TranslationUnitDecl\",\"inner\":[{\"kind\":\"FunctionDecl\",\"name\":\"cipher2_toolchain_probe\",\"loc\":{\"line\":1},\"isThisDeclarationADefinition\":true}]}'\n",
        encoding="utf-8",
    )
    gcc.write_text("#!/bin/sh\necho 'gcc (GCC) 10.5.0'\n", encoding="utf-8")
    clang.chmod(0o755)
    gcc.chmod(0o755)
    write_default_config(target, clang_executable="bin/clang", gcc_executable="bin/gcc", observe=False)


if __name__ == "__main__":
    main_gate()

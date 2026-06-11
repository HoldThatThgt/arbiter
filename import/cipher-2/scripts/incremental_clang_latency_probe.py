from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional


SCENARIOS = [
    ("single_translation_unit", "src/unit.c", "int helper(void) { return 1; }\n"),
    ("header_fanout", "include/common.h", "#define VALUE 1\n"),
    ("failure_fallback", "src/broken.c", "int broken(void) { return ;\n"),
]


def main() -> None:
    clang = shutil.which("clang")
    gcc = shutil.which("gcc")
    toolchain = _toolchain_status(clang, gcc)
    results: List[Dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        (target / "src").mkdir()
        (target / "include").mkdir()
        for name, rel_path, content in SCENARIOS:
            path = target / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if toolchain["status"] != "available" or clang is None:
                results.append({"scenario": name, "status": "skipped", "reason": toolchain["status"]})
                continue
            started = time.perf_counter()
            completed = subprocess.run(
                [
                    clang,
                    "-Xclang",
                    "-ast-dump=json",
                    "-fsyntax-only",
                    "-I",
                    str(target / "include"),
                    str(path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            results.append(
                {
                    "scenario": name,
                    "status": "ok" if completed.returncode == 0 else "clang_failed",
                    "returncode": completed.returncode,
                    "elapsed_ms": round(elapsed_ms, 3),
                }
            )
    print(json.dumps({"toolchain": toolchain, "incremental_clang_latency_probe": results}, ensure_ascii=False, sort_keys=True))


def _toolchain_status(clang: Optional[str], gcc: Optional[str]) -> Dict[str, object]:
    if clang is None:
        return {"status": "clang_unavailable", "clang": None, "gcc": gcc}
    if gcc is None:
        return {"status": "gcc_unavailable", "clang": clang, "gcc": None}
    clang_version = _version_output(clang)
    gcc_version = _version_output(gcc)
    clang_ok = "clang version 16." in clang_version or "LLVM version 16." in clang_version
    gcc_ok = "10.5.0" in gcc_version
    if not clang_ok:
        status = "clang_version_mismatch"
    elif not gcc_ok:
        status = "gcc_version_mismatch"
    else:
        status = "available"
    return {
        "status": status,
        "clang": clang,
        "gcc": gcc,
        "clang_version": _first_line(clang_version),
        "gcc_version": _first_line(gcc_version),
    }


def _version_output(executable: str) -> str:
    try:
        return subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text.splitlines() else ""


if __name__ == "__main__":
    main()

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__


def doctor(root: Path, *, gdb: Optional[str]) -> Dict[str, Any]:
    resolved = root.expanduser().resolve()
    checks = []
    checks.append({"name": "python", "ok": True, "detail": sys.executable})
    checks.append({"name": "package", "ok": True, "detail": f"arbiter_engine.gdbmcp {__version__}"})
    gdb_path = gdb or os.environ.get("GDB_MCP_GDB") or "gdb"
    found = shutil.which(gdb_path) or (str(Path(gdb_path).expanduser()) if Path(gdb_path).expanduser().exists() else None)
    if found:
        detail = found
        try:
            proc = subprocess.run([found, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5)
            first = proc.stdout.splitlines()[0] if proc.stdout else ""
            detail = f"{found} ({first})"
        except Exception as exc:
            detail = f"{found} (version check failed: {exc})"
        checks.append({"name": "gdb", "ok": True, "detail": detail})
        checks.append(gdb_run_check(found))
    else:
        checks.append({"name": "gdb", "ok": False, "detail": f"not found: {gdb_path}"})
    checks.append({"name": "root", "ok": resolved.exists() and resolved.is_dir(), "detail": str(resolved)})
    ok = all(bool(check["ok"]) for check in checks)
    return {"ok": ok, "checks": checks}


def gdb_run_check(gdb_path: str) -> Dict[str, Any]:
    cc = shutil.which("cc")
    if not cc:
        return {"name": "gdb_run", "ok": False, "detail": "cannot probe local inferior run support: cc not found"}
    # A diagnostics probe must always REPORT, never crash the diagnostics call: on a slow or
    # heavily contended host the compile or gdb-start can exceed the deadline (or fail to spawn).
    # Treat that as a not-ok signal rather than letting the exception escape doctor(). Deadlines
    # are generous so a healthy-but-loaded host still completes the probe.
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "probe.c"
            binary = root / "probe"
            source.write_text("int main(void) { int x = 42; return x == 42 ? 0 : 1; }\n", encoding="utf-8")
            compile_cmd = [cc, "-g", "-gdwarf-4", "-O0", str(source), "-o", str(binary)]
            compiled = subprocess.run(compile_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            if compiled.returncode != 0:
                return {
                    "name": "gdb_run",
                    "ok": False,
                    "detail": "probe compile failed: " + _first_line(compiled.stdout),
                }
            probe = subprocess.run(
                [gdb_path, "--batch", "-q", str(binary), "-ex", "start", "-ex", "quit"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            output = probe.stdout or ""
            if probe.returncode == 0 and "Don't know how to run" not in output and "Unable to find Mach task port" not in output:
                return {"name": "gdb_run", "ok": True, "detail": "local inferior run probe passed"}
            return {
                "name": "gdb_run",
                "ok": False,
                "detail": _first_line(output) or f"probe failed with exit code {probe.returncode}",
            }
    except subprocess.TimeoutExpired:
        return {"name": "gdb_run", "ok": False, "detail": "local inferior run probe timed out (slow or contended host)"}
    except OSError as exc:
        return {"name": "gdb_run", "ok": False, "detail": f"local inferior run probe could not start: {exc}"}


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return ""

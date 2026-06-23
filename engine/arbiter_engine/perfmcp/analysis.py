from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from . import __version__
from .schema import (
    CONFIDENCE_RANK,
    RULES,
    SCHEMA_VERSION,
    SEVERITY_RANK,
    Finding,
    Location,
    normalize_confidence,
    normalize_severity,
)


C_EXTENSIONS = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"}
DEFAULT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "build",
    "cmake-build-debug",
    "cmake-build-release",
    "dist",
    "node_modules",
    "out",
    "target",
    "venv",
    ".venv",
}
TEST_PATH_PARTS = {"test", "tests", "testing", "spec", "specs"}


@dataclass(frozen=True)
class LoopSpan:
    kind: str
    start: int
    end: int
    header_end: int
    start_line: int
    end_line: int
    depth: int
    header: str


@dataclass(frozen=True)
class FunctionSpan:
    name: str
    start: int
    end: int


def scan_c_project(
    root: str | os.PathLike[str] | None = None,
    paths: list[str] | None = None,
    max_findings: int = 50,
    min_severity: str = "medium",
    include_tests: bool = False,
    include_low_confidence: bool = False,
    budget_files: int = 250,
    budget_bytes: int = 5_000_000,
) -> dict[str, Any]:
    root_path = _resolve_root(root)
    max_findings = max(1, min(int(max_findings), 500))
    budget_files = max(1, min(int(budget_files), 10_000))
    budget_bytes = max(64_000, min(int(budget_bytes), 250_000_000))
    min_severity = normalize_severity(min_severity, "medium")

    files, warnings = _discover_files(root_path, paths, include_tests, budget_files, budget_bytes)
    findings: list[Finding] = []
    bytes_scanned = 0
    files_scanned = 0

    for file_path in files:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"Could not read {file_path}: {exc}")
            continue
        bytes_scanned += len(content.encode("utf-8", errors="replace"))
        files_scanned += 1
        rel_path = _display_path(root_path, file_path)
        findings.extend(_scan_file(rel_path, content))

    findings = _filter_and_rank(findings, min_severity, include_low_confidence)
    truncated = len(findings) > max_findings
    findings = findings[:max_findings]
    for index, finding in enumerate(findings, start=1):
        finding.id = f"PERF{index:04d}"

    summary = {
        "finding_count": len(findings),
        "truncated": truncated,
        "by_severity": _count_by(findings, "severity"),
        "by_rule": _count_by(findings, "rule_id"),
        "highest_severity": findings[0].severity if findings else None,
    }
    config = {
        "root": str(root_path),
        "paths": paths or [],
        "max_findings": max_findings,
        "min_severity": min_severity,
        "include_tests": include_tests,
        "include_low_confidence": include_low_confidence,
        "budget_files": budget_files,
        "budget_bytes": budget_bytes,
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "analysis_id": _analysis_id(root_path, files, config),
        "root": str(root_path),
        "config": config,
        "summary": summary,
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "warnings": warnings,
        "findings": [finding.to_dict() for finding in findings],
    }
    return payload


def explain_rule_or_finding(
    rule_id: str | None = None,
    finding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_rule = rule_id
    if finding:
        selected_rule = str(finding.get("rule_id") or selected_rule or "")
    if not selected_rule:
        return {
            "schema_version": "perf-mcp.explain.v1",
            "is_error": True,
            "message": "Provide rule_id or a finding object with rule_id.",
        }
    rule = RULES.get(selected_rule)
    if not rule:
        return {
            "schema_version": "perf-mcp.explain.v1",
            "is_error": True,
            "message": f"Unknown rule_id: {selected_rule}",
            "known_rule_ids": sorted(RULES),
        }

    explanation = rule.to_explanation()
    explanation["schema_version"] = "perf-mcp.explain.v1"
    explanation["tool_version"] = __version__
    if finding:
        explanation["finding"] = finding
        explanation["next_agent_steps"] = _finding_next_steps(finding)
    return explanation


def probe_toolchain(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    root_path = _resolve_root(root)
    tools = {}
    for name in (
        "cc",
        "clang",
        "gcc",
        "make",
        "cmake",
        "ninja",
        "perf",
        "valgrind",
        "gprof",
        "hyperfine",
        "dtrace",
        "xctrace",
        "time",
    ):
        path = shutil.which(name)
        tools[name] = {"available": bool(path), "path": path}

    recommendations: list[str] = []
    if tools["perf"]["available"]:
        recommendations.append("Use perf stat/record for Linux CPU counters and sampled hotspots.")
    if tools["dtrace"]["available"]:
        recommendations.append("Use dtrace for syscall and user/kernel probes when platform permissions allow it.")
    if tools["xctrace"]["available"]:
        recommendations.append("Use xctrace/Instruments for macOS time profiler evidence.")
    if tools["hyperfine"]["available"]:
        recommendations.append("Use hyperfine for statistically cleaner command benchmarks.")
    recommendations.append("Use perf.measure_command for a dependency-free wall/user/system time baseline.")

    return {
        "schema_version": "perf-mcp.probe.v1",
        "tool_version": __version__,
        "root": str(root_path),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "tools": tools,
        "recommendations": recommendations,
    }


def measure_command(
    command: list[str],
    cwd: str | os.PathLike[str] | None = None,
    repeat: int = 3,
    timeout_seconds: float = 60.0,
    env: dict[str, str] | None = None,
    max_output_chars: int = 20_000,
) -> dict[str, Any]:
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        return {
            "schema_version": "perf-mcp.measure.v1",
            "is_error": True,
            "message": "command must be a non-empty array of strings; shell strings are intentionally not accepted.",
        }
    repeat = max(1, min(int(repeat), 30))
    timeout_seconds = max(0.1, min(float(timeout_seconds), 3600.0))
    max_output_chars = max(1000, min(int(max_output_chars), 200_000))
    cwd_path = _resolve_root(cwd)

    merged_env = os.environ.copy()
    if env:
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                return {
                    "schema_version": "perf-mcp.measure.v1",
                    "is_error": True,
                    "message": "env must be an object with string keys and string values.",
                }
            merged_env[key] = value

    runs: list[dict[str, Any]] = []
    for index in range(1, repeat + 1):
        runs.append(_run_once(command, cwd_path, timeout_seconds, merged_env, max_output_chars, index))

    completed = [run for run in runs if not run["timed_out"] and run["exit_code"] == 0]
    wall_values = [run["wall_seconds"] for run in completed]
    summary = {
        "repeat": repeat,
        "successful_runs": len(completed),
        "all_successful": len(completed) == repeat,
        "median_wall_seconds": median(wall_values) if wall_values else None,
        "min_wall_seconds": min(wall_values) if wall_values else None,
        "max_wall_seconds": max(wall_values) if wall_values else None,
    }
    return {
        "schema_version": "perf-mcp.measure.v1",
        "tool_version": __version__,
        "command": command,
        "cwd": str(cwd_path),
        "timeout_seconds": timeout_seconds,
        "summary": summary,
        "runs": runs,
    }


def _scan_file(path: str, content: str) -> list[Finding]:
    sanitized = sanitize_c(content)
    line_offsets = _line_offsets(sanitized)
    loops = _detect_loops(sanitized, line_offsets)
    functions = _detect_functions(sanitized)
    original_lines = content.splitlines()
    findings: list[Finding] = []
    seen: set[tuple[str, int, str]] = set()

    for loop in loops:
        if loop.depth >= 1:
            severity = "high" if loop.depth >= 2 else "medium"
            _add_finding(
                findings,
                seen,
                "C.PERF.NESTED_LOOP",
                path,
                loop.start,
                line_offsets,
                functions,
                original_lines,
                severity=severity,
                confidence="medium",
                loop=loop,
            )

        region = sanitized[loop.start : loop.end]
        header_region = sanitized[loop.start : loop.header_end]
        for match in re.finditer(r"\bstrlen\s*\(", region):
            absolute = loop.start + match.start()
            in_header = loop.start <= absolute <= loop.header_end
            _add_finding(
                findings,
                seen,
                "C.PERF.STRLEN_IN_LOOP",
                path,
                absolute,
                line_offsets,
                functions,
                original_lines,
                severity="medium",
                confidence="high" if in_header or "strlen" in header_region else "medium",
                loop=loop,
            )

        for match in re.finditer(r"\b(calloc|malloc|free)\s*\(", region):
            _add_finding(
                findings,
                seen,
                "C.PERF.ALLOC_IN_LOOP",
                path,
                loop.start + match.start(),
                line_offsets,
                functions,
                original_lines,
                severity="high",
                confidence="high",
                loop=loop,
                matched_symbol=match.group(1),
            )

        for match in re.finditer(r"\brealloc\s*\(", region):
            absolute = loop.start + match.start()
            line = _line_col(line_offsets, absolute)[0]
            snippet = _snippet(original_lines, line)
            grow_one = bool(re.search(r"(\+\+|--|\+\s*1|-\s*1|\+=\s*1|count\s*\+|len\s*\+)", snippet))
            _add_finding(
                findings,
                seen,
                "C.PERF.REALLOC_GROW_ONE" if grow_one else "C.PERF.ALLOC_IN_LOOP",
                path,
                absolute,
                line_offsets,
                functions,
                original_lines,
                severity="high",
                confidence="high" if grow_one else "medium",
                loop=loop,
                matched_symbol="realloc",
            )

        for match in re.finditer(r"\b(memcpy|memmove|memset|strcpy|strncpy|strcat|snprintf|sprintf)\s*\(", region):
            _add_finding(
                findings,
                seen,
                "C.PERF.BULK_MEMORY_IN_LOOP",
                path,
                loop.start + match.start(),
                line_offsets,
                functions,
                original_lines,
                severity="high" if loop.depth >= 1 else "medium",
                confidence="medium",
                loop=loop,
                matched_symbol=match.group(1),
            )

        for match in re.finditer(r"\b(printf|fprintf|fputs|puts|scanf|fscanf|read|write|fread|fwrite)\s*\(", region):
            _add_finding(
                findings,
                seen,
                "C.PERF.IO_IN_LOOP",
                path,
                loop.start + match.start(),
                line_offsets,
                functions,
                original_lines,
                severity="medium",
                confidence="medium",
                loop=loop,
                matched_symbol=match.group(1),
            )

        for match in re.finditer(r"\b(pow|sqrt|sin|cos|tan|log|exp)\s*\(", region):
            _add_finding(
                findings,
                seen,
                "C.PERF.EXPENSIVE_MATH_IN_LOOP",
                path,
                loop.start + match.start(),
                line_offsets,
                functions,
                original_lines,
                severity="low",
                confidence="medium",
                loop=loop,
                matched_symbol=match.group(1),
            )

    return findings


def sanitize_c(content: str) -> str:
    out: list[str] = []
    state = "normal"
    escape = False
    index = 0
    while index < len(content):
        char = content[index]
        nxt = content[index + 1] if index + 1 < len(content) else ""
        if state == "normal":
            if char == "/" and nxt == "/":
                out.extend((" ", " "))
                index += 2
                state = "line_comment"
                continue
            if char == "/" and nxt == "*":
                out.extend((" ", " "))
                index += 2
                state = "block_comment"
                continue
            if char == '"':
                out.append(" ")
                state = "string"
                escape = False
                index += 1
                continue
            if char == "'":
                out.append(" ")
                state = "char"
                escape = False
                index += 1
                continue
            out.append(char)
            index += 1
            continue
        if state == "line_comment":
            if char == "\n":
                out.append("\n")
                state = "normal"
            else:
                out.append(" ")
            index += 1
            continue
        if state == "block_comment":
            if char == "\n":
                out.append("\n")
                index += 1
                continue
            if char == "*" and nxt == "/":
                out.extend((" ", " "))
                index += 2
                state = "normal"
                continue
            out.append(" ")
            index += 1
            continue
        if state in {"string", "char"}:
            if char == "\n":
                out.append("\n")
                state = "normal"
                escape = False
                index += 1
                continue
            terminator = '"' if state == "string" else "'"
            if escape:
                out.append(" ")
                escape = False
                index += 1
                continue
            if char == "\\":
                out.append(" ")
                escape = True
                index += 1
                continue
            if char == terminator:
                out.append(" ")
                state = "normal"
                index += 1
                continue
            out.append(" ")
            index += 1
            continue
    return "".join(out)


def _detect_loops(text: str, line_offsets: list[int]) -> list[LoopSpan]:
    loops: list[LoopSpan] = []
    for match in re.finditer(r"\b(for|while)\s*\(", text):
        kind = match.group(1)
        paren_start = text.find("(", match.start())
        paren_end = _find_matching(text, paren_start, "(", ")")
        if paren_end is None:
            continue
        body_start = _skip_ws(text, paren_end + 1)
        if body_start < len(text) and text[body_start] == "{":
            body_end = _find_matching(text, body_start, "{", "}")
            if body_end is None:
                body_end = _line_end(text, body_start)
            else:
                body_end += 1
        else:
            body_end = _statement_end(text, body_start)
        start_line = _line_col(line_offsets, match.start())[0]
        end_line = _line_col(line_offsets, max(match.start(), body_end - 1))[0]
        header = " ".join(text[match.start() : paren_end + 1].split())
        loops.append(
            LoopSpan(
                kind=kind,
                start=match.start(),
                end=body_end,
                header_end=paren_end,
                start_line=start_line,
                end_line=end_line,
                depth=0,
                header=header,
            )
        )

    loops.sort(key=lambda loop: (loop.start, loop.end))
    with_depth: list[LoopSpan] = []
    for loop in loops:
        depth = sum(1 for other in loops if other.start < loop.start and loop.end <= other.end)
        with_depth.append(
            LoopSpan(
                kind=loop.kind,
                start=loop.start,
                end=loop.end,
                header_end=loop.header_end,
                start_line=loop.start_line,
                end_line=loop.end_line,
                depth=depth,
                header=loop.header,
            )
        )
    return with_depth


def _detect_functions(text: str) -> list[FunctionSpan]:
    functions: list[FunctionSpan] = []
    pattern = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*\{", re.MULTILINE)
    for match in pattern.finditer(text):
        name = match.group(1)
        if name in {"if", "for", "while", "switch", "catch"}:
            continue
        brace = text.find("{", match.start(), match.end())
        if brace < 0:
            continue
        end = _find_matching(text, brace, "{", "}")
        if end is None:
            continue
        prefix = text[max(0, match.start() - 80) : match.start()]
        if "=" in prefix.rsplit("\n", 1)[-1]:
            continue
        functions.append(FunctionSpan(name=name, start=match.start(), end=end + 1))
    return functions


def _add_finding(
    findings: list[Finding],
    seen: set[tuple[str, int, str]],
    rule_id: str,
    path: str,
    offset: int,
    line_offsets: list[int],
    functions: list[FunctionSpan],
    original_lines: list[str],
    severity: str,
    confidence: str,
    loop: LoopSpan,
    matched_symbol: str | None = None,
) -> None:
    line, column = _line_col(line_offsets, offset)
    key = (rule_id, line, matched_symbol or "")
    if key in seen:
        return
    seen.add(key)
    rule = RULES[rule_id]
    function = _function_at(functions, offset)
    evidence: dict[str, Any] = {
        "snippet": _snippet(original_lines, line),
        "loop_start_line": loop.start_line,
        "loop_end_line": loop.end_line,
        "loop_depth": loop.depth + 1,
        "loop_header": loop.header[:240],
    }
    if matched_symbol:
        evidence["matched_symbol"] = matched_symbol
    findings.append(
        Finding(
            rule_id=rule_id,
            title=rule.title,
            severity=normalize_severity(severity, rule.default_severity),
            confidence=normalize_confidence(confidence, rule.default_confidence),
            location=Location(path=path, line=line, column=column, function=function),
            evidence=evidence,
            impact=rule.impact,
            recommendation=rule.recommendation,
            tags=list(rule.tags),
            agent_hints={
                "verification": "Confirm the path is hot with a benchmark or profiler before landing non-trivial rewrites.",
                "safe_refactor": "Prefer small behavior-preserving patches with before/after measurements.",
            },
        )
    )


def _filter_and_rank(findings: list[Finding], min_severity: str, include_low_confidence: bool) -> list[Finding]:
    min_rank = SEVERITY_RANK[min_severity]
    filtered = [
        finding
        for finding in findings
        if SEVERITY_RANK[finding.severity] >= min_rank
        and (include_low_confidence or CONFIDENCE_RANK[finding.confidence] >= CONFIDENCE_RANK["medium"])
    ]
    return sorted(
        filtered,
        key=lambda finding: (
            -SEVERITY_RANK[finding.severity],
            -CONFIDENCE_RANK[finding.confidence],
            finding.location.path,
            finding.location.line,
            finding.location.column,
            finding.rule_id,
        ),
    )


def _discover_files(
    root: Path,
    paths: list[str] | None,
    include_tests: bool,
    budget_files: int,
    budget_bytes: int,
) -> tuple[list[Path], list[str]]:
    warnings: list[str] = []
    candidates: list[Path] = []
    roots = [_safe_child(root, value, warnings) for value in paths] if paths else [root]
    byte_total = 0
    for item in roots:
        if item is None:
            continue
        if item.is_file():
            iterable: Iterable[Path] = [item]
        elif item.is_dir():
            iterable = _walk_files(item, include_tests)
        else:
            warnings.append(f"Path does not exist or is not readable: {item}")
            continue
        for file_path in iterable:
            if file_path.suffix.lower() not in C_EXTENSIONS:
                continue
            if not include_tests and _is_test_path(root, file_path):
                continue
            try:
                size = file_path.stat().st_size
            except OSError as exc:
                warnings.append(f"Could not stat {file_path}: {exc}")
                continue
            if len(candidates) >= budget_files:
                warnings.append(f"File budget reached at {budget_files} C/C++ files.")
                return sorted(candidates), warnings
            if byte_total + size > budget_bytes:
                warnings.append(f"Byte budget reached at {budget_bytes} bytes.")
                return sorted(candidates), warnings
            byte_total += size
            candidates.append(file_path)
    return sorted(set(candidates)), warnings


def _walk_files(root: Path, include_tests: bool) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in DEFAULT_SKIP_DIRS and (include_tests or name.lower() not in TEST_PATH_PARTS)
        ]
        for filename in filenames:
            yield Path(dirpath) / filename


def _safe_child(root: Path, raw_path: str, warnings: list[str]) -> Path | None:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        warnings.append(f"Could not resolve {raw_path}: {exc}")
        return None
    if not _is_relative_to(resolved, root):
        warnings.append(f"Skipping path outside root: {raw_path}")
        return None
    return resolved


def _is_test_path(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    lowered = {part.lower() for part in parts[:-1]}
    basename = path.name.lower()
    return bool(lowered & TEST_PATH_PARTS) or basename.startswith("test_") or basename.endswith("_test.c")


def _analysis_id(root: Path, files: list[Path], config: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(str(root).encode("utf-8"))
    digest.update(json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for file_path in sorted(files):
        digest.update(str(file_path).encode("utf-8"))
        try:
            stat = file_path.stat()
        except OSError:
            continue
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(int(stat.st_mtime_ns)).encode("ascii"))
    return digest.hexdigest()[:16]


def _run_once(
    command: list[str],
    cwd: Path,
    timeout_seconds: float,
    env: dict[str, str],
    max_output_chars: int,
    index: int,
) -> dict[str, Any]:
    try:
        import resource
    except ImportError:  # pragma: no cover - resource is available on supported POSIX targets.
        resource = None  # type: ignore[assignment]

    before_usage = resource.getrusage(resource.RUSAGE_CHILDREN) if resource else None
    started = time.perf_counter()
    timed_out = False
    # start_new_session=True puts the child in its own process group so that, on
    # timeout, os.killpg reaps the whole tree (grandchildren too), not just the
    # direct child as plain subprocess.run would.
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        exit_code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        # Bound the post-kill drain: if a survivor (e.g. a grandchild that escaped the
        # group) still holds the pipe open, do not let communicate() block the server.
        try:
            stdout, stderr = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    wall = time.perf_counter() - started
    after_usage = resource.getrusage(resource.RUSAGE_CHILDREN) if resource else None
    user_seconds = None
    system_seconds = None
    max_rss_kb = None
    if before_usage and after_usage:
        user_seconds = max(0.0, after_usage.ru_utime - before_usage.ru_utime)
        system_seconds = max(0.0, after_usage.ru_stime - before_usage.ru_stime)
        max_rss_kb = _rss_to_kb(after_usage.ru_maxrss)

    return {
        "run": index,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "wall_seconds": wall,
        "user_seconds": user_seconds,
        "system_seconds": system_seconds,
        "max_rss_kb": max_rss_kb,
        "stdout": _truncate(stdout, max_output_chars),
        "stderr": _truncate(stderr, max_output_chars),
    }


def _rss_to_kb(value: int) -> int:
    if platform.system() == "Darwin":
        return int(value / 1024)
    return int(value)


def _truncate(value: str | None, max_chars: int) -> str:
    value = value or ""
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 80] + f"\n... truncated {len(value) - max_chars + 80} chars ..."


def _finding_next_steps(finding: dict[str, Any]) -> list[str]:
    location = finding.get("location", {})
    path = location.get("path", "<unknown>")
    line = location.get("line", "?")
    return [
        f"Open {path}:{line} and verify the loop bounds and data sizes.",
        "Add or locate a workload that exercises this path at realistic scale.",
        "Use perf.measure_command to capture before/after runtime for the smallest safe patch.",
    ]


def _resolve_root(root: str | os.PathLike[str] | None) -> Path:
    if root is None:
        root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(root).expanduser().resolve()


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", text):
        offsets.append(match.end())
    return offsets


def _line_col(line_offsets: list[int], offset: int) -> tuple[int, int]:
    low = 0
    high = len(line_offsets)
    while low + 1 < high:
        mid = (low + high) // 2
        if line_offsets[mid] <= offset:
            low = mid
        else:
            high = mid
    return low + 1, offset - line_offsets[low] + 1


def _snippet(lines: list[str], line: int) -> str:
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()[:240]
    return ""


def _function_at(functions: list[FunctionSpan], offset: int) -> str | None:
    matches = [function for function in functions if function.start <= offset <= function.end]
    if not matches:
        return None
    return max(matches, key=lambda function: function.start).name


def _find_matching(text: str, start: int, opener: str, closer: str) -> int | None:
    if start < 0 or start >= len(text) or text[start] != opener:
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
    return None


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _statement_end(text: str, index: int) -> int:
    semi = text.find(";", index)
    newline = text.find("\n", index)
    candidates = [candidate for candidate in (semi, newline) if candidate >= 0]
    if not candidates:
        return min(len(text), index + 1)
    return min(candidates) + 1


def _line_end(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline


def _count_by(findings: list[Finding], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        value = getattr(finding, field)
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

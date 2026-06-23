from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Callable

from .analysis import explain_rule_or_finding, measure_command, probe_toolchain, scan_c_project


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]

_SCAN_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_MAX_SCAN_CACHE = 8


SCAN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root": {
            "type": "string",
            "description": "Project root. Defaults to CLAUDE_PROJECT_DIR, then the server working directory.",
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional project-relative files or directories to scan.",
        },
        "max_findings": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 50,
            "description": "Maximum ranked findings to return.",
        },
        "min_severity": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "default": "medium",
            "description": "Filter out findings below this severity.",
        },
        "include_tests": {
            "type": "boolean",
            "default": False,
            "description": "Include files in test directories and test-like filenames.",
        },
        "include_low_confidence": {
            "type": "boolean",
            "default": False,
            "description": "Include low-confidence findings.",
        },
        "budget_files": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000,
            "default": 250,
            "description": "Hard cap on the number of C/C++ files scanned.",
        },
        "budget_bytes": {
            "type": "integer",
            "minimum": 64000,
            "maximum": 250000000,
            "default": 5000000,
            "description": "Hard cap on bytes scanned.",
        },
    },
    "additionalProperties": False,
}


EXPLAIN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "analysis_id": {
            "type": "string",
            "description": "Optional analysis_id returned by perf.scan_c.",
        },
        "finding_id": {
            "type": "string",
            "description": "Optional finding id returned by perf.scan_c, for example PERF0001.",
        },
        "rule_id": {
            "type": "string",
            "description": "Rule id to explain when no cached finding is provided.",
        },
        "finding": {
            "type": "object",
            "description": "Full finding object returned by perf.scan_c.",
        },
    },
    "additionalProperties": False,
}


MEASURE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Command argv to execute. Shell strings are not accepted.",
        },
        "cwd": {
            "type": "string",
            "description": "Working directory. Defaults to CLAUDE_PROJECT_DIR, then the server working directory.",
        },
        "repeat": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30,
            "default": 3,
            "description": "Number of repetitions.",
        },
        "timeout_seconds": {
            "type": "number",
            "minimum": 0.1,
            "maximum": 3600,
            "default": 60,
            "description": "Per-run timeout.",
        },
        "env": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Extra environment variables for the command.",
        },
        "max_output_chars": {
            "type": "integer",
            "minimum": 1000,
            "maximum": 200000,
            "default": 20000,
            "description": "Captured stdout/stderr cap per stream.",
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}


ROOT_ONLY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root": {
            "type": "string",
            "description": "Project root. Defaults to CLAUDE_PROJECT_DIR, then the server working directory.",
        }
    },
    "additionalProperties": False,
}


GENERIC_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "perf.scan_c",
        "title": "Scan C Performance Risks",
        "description": (
            "Scan C/C++ source for likely performance risks in hot paths. Returns ranked, "
            "schema-versioned findings with file/line evidence and remediation hints."
        ),
        "inputSchema": SCAN_INPUT_SCHEMA,
        "outputSchema": GENERIC_OUTPUT_SCHEMA,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        "_meta": {"anthropic/maxResultSizeChars": 120000},
    },
    {
        "name": "perf.explain_finding",
        "title": "Explain Perf Finding",
        "description": (
            "Explain a perf.scan_c finding or rule id, including false-positive checks, "
            "safe refactor strategy, and measurement plan."
        ),
        "inputSchema": EXPLAIN_INPUT_SCHEMA,
        "outputSchema": GENERIC_OUTPUT_SCHEMA,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "perf.measure_command",
        "title": "Measure Command",
        "description": (
            "Run an argv command repeatedly with wall/user/system timing, timeout, exit code, "
            "and bounded stdout/stderr capture. Use for before/after performance evidence."
        ),
        "inputSchema": MEASURE_INPUT_SCHEMA,
        "outputSchema": GENERIC_OUTPUT_SCHEMA,
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
        "_meta": {"anthropic/maxResultSizeChars": 120000},
    },
    {
        "name": "perf.toolchain_probe",
        "title": "Probe Perf Toolchain",
        "description": (
            "Report available local compiler/profiler tools and suggest the best measurement "
            "path for the current platform."
        ),
        "inputSchema": ROOT_ONLY_INPUT_SCHEMA,
        "outputSchema": GENERIC_OUTPUT_SCHEMA,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
]


def list_tools() -> list[dict[str, Any]]:
    return TOOLS


def tool_schema(name: str) -> dict[str, Any]:
    for tool in TOOLS:
        if tool["name"] == name:
            return tool["inputSchema"]
    raise KeyError(name)


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return tool_error("Tool arguments must be an object.")
    handlers: dict[str, ToolHandler] = {
        "perf.scan_c": _handle_scan,
        "perf.explain_finding": _handle_explain,
        "perf.measure_command": _handle_measure,
        "perf.toolchain_probe": _handle_probe,
    }
    handler = handlers.get(name)
    if not handler:
        raise KeyError(name)
    try:
        payload = handler(arguments)
    except Exception as exc:  # pragma: no cover - exercised through MCP integration tests.
        return tool_error(f"{type(exc).__name__}: {exc}")
    if payload.get("is_error"):
        return tool_error(str(payload.get("message", "Tool execution failed.")), payload)
    return tool_success(payload)


def tool_success(payload: dict[str, Any], summary: str | None = None) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": summary or _summarize_payload(payload)}],
        "structuredContent": payload,
        "isError": False,
    }


def tool_error(message: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    structured = payload or {"is_error": True, "message": message}
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": structured,
        "isError": True,
    }


def _handle_scan(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = scan_c_project(
        root=arguments.get("root"),
        paths=_string_list(arguments.get("paths")),
        max_findings=int(arguments.get("max_findings", 50)),
        min_severity=str(arguments.get("min_severity", "medium")),
        include_tests=bool(arguments.get("include_tests", False)),
        include_low_confidence=bool(arguments.get("include_low_confidence", False)),
        budget_files=int(arguments.get("budget_files", 250)),
        budget_bytes=int(arguments.get("budget_bytes", 5_000_000)),
    )
    _cache_scan(payload)
    return payload


def _handle_explain(arguments: dict[str, Any]) -> dict[str, Any]:
    finding = arguments.get("finding")
    analysis_id = arguments.get("analysis_id")
    finding_id = arguments.get("finding_id")
    if not finding and analysis_id and finding_id:
        cached = _SCAN_CACHE.get(str(analysis_id))
        if not cached:
            return {
                "schema_version": "perf-mcp.explain.v1",
                "is_error": True,
                "message": f"Unknown analysis_id in this server process: {analysis_id}",
            }
        finding = next(
            (item for item in cached.get("findings", []) if item.get("id") == finding_id),
            None,
        )
        if not finding:
            return {
                "schema_version": "perf-mcp.explain.v1",
                "is_error": True,
                "message": f"Unknown finding_id for analysis {analysis_id}: {finding_id}",
            }
    if finding is not None and not isinstance(finding, dict):
        return {
            "schema_version": "perf-mcp.explain.v1",
            "is_error": True,
            "message": "finding must be an object.",
        }
    return explain_rule_or_finding(rule_id=arguments.get("rule_id"), finding=finding)


def _handle_measure(arguments: dict[str, Any]) -> dict[str, Any]:
    return measure_command(
        command=arguments.get("command"),
        cwd=arguments.get("cwd"),
        repeat=int(arguments.get("repeat", 3)),
        timeout_seconds=float(arguments.get("timeout_seconds", 60.0)),
        env=arguments.get("env"),
        max_output_chars=int(arguments.get("max_output_chars", 20_000)),
    )


def _handle_probe(arguments: dict[str, Any]) -> dict[str, Any]:
    return probe_toolchain(root=arguments.get("root"))


def _cache_scan(payload: dict[str, Any]) -> None:
    analysis_id = payload.get("analysis_id")
    if not analysis_id:
        return
    key = str(analysis_id)
    _SCAN_CACHE[key] = payload
    _SCAN_CACHE.move_to_end(key)
    while len(_SCAN_CACHE) > _MAX_SCAN_CACHE:
        _SCAN_CACHE.popitem(last=False)


def _string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    return [str(item) for item in value]


def _summarize_payload(payload: dict[str, Any]) -> str:
    schema = payload.get("schema_version")
    if schema == "perf-mcp.scan.v1":
        summary = payload.get("summary", {})
        count = summary.get("finding_count", 0)
        files = payload.get("files_scanned", 0)
        top = payload.get("findings", [])[:5]
        lines = [f"perf.scan_c: {count} findings across {files} files."]
        for finding in top:
            location = finding.get("location", {})
            lines.append(
                "- {id} {severity}/{confidence} {rule} at {path}:{line}: {title}".format(
                    id=finding.get("id"),
                    severity=finding.get("severity"),
                    confidence=finding.get("confidence"),
                    rule=finding.get("rule_id"),
                    path=location.get("path"),
                    line=location.get("line"),
                    title=finding.get("title"),
                )
            )
        return "\n".join(lines)
    if schema == "perf-mcp.measure.v1":
        summary = payload.get("summary", {})
        return "perf.measure_command: {ok}/{repeat} successful, median wall={median}".format(
            ok=summary.get("successful_runs"),
            repeat=summary.get("repeat"),
            median=summary.get("median_wall_seconds"),
        )
    if schema == "perf-mcp.probe.v1":
        available = [
            name
            for name, info in payload.get("tools", {}).items()
            if isinstance(info, dict) and info.get("available")
        ]
        return "perf.toolchain_probe: available tools: " + ", ".join(available)
    if schema == "perf-mcp.explain.v1":
        return "perf.explain_finding: " + str(payload.get("title", payload.get("rule_id", "rule explanation")))
    return json.dumps(payload, sort_keys=True)[:4000]

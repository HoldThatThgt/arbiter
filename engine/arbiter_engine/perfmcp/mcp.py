from __future__ import annotations

import json
import sys
import traceback
from typing import Any, TextIO

from . import __version__
from .tools import call_tool, list_tools


SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class MCPServer:
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC request.")
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message

        if not isinstance(method, str):
            if is_notification:
                return None
            return self._error(request_id, JSONRPC_INVALID_REQUEST, "Missing JSON-RPC method.")

        if is_notification:
            return None

        try:
            if method == "initialize":
                return self._result(request_id, self._initialize(message.get("params") or {}))
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                return self._result(request_id, {"tools": list_tools()})
            if method == "tools/call":
                return self._result(request_id, self._tools_call(message.get("params") or {}))
            return self._error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")
        except KeyError as exc:
            return self._error(request_id, JSONRPC_INVALID_PARAMS, f"Unknown tool: {exc.args[0]}")
        except ValueError as exc:
            return self._error(request_id, JSONRPC_INVALID_PARAMS, str(exc))
        except Exception as exc:  # pragma: no cover - defensive boundary for stdio server.
            print(traceback.format_exc(), file=sys.stderr)
            return self._error(request_id, JSONRPC_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "perf-mcp",
                "title": "Perf MCP",
                "version": __version__,
                "description": "C performance triage and measurement tools for coding agents.",
            },
            "instructions": (
                "Use perf.scan_c for ranked C performance findings, perf.explain_finding for "
                "safe remediation guidance, perf.measure_command for before/after evidence, "
                "and perf.toolchain_probe to choose a profiling path."
            ),
        }

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise ValueError("tools/call requires params.name.")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("tools/call params.arguments must be an object.")
        return call_tool(name, arguments)

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def serve_stdio(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = MCPServer()
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(stdout, MCPServer._error(None, JSONRPC_PARSE_ERROR, f"Parse error: {exc.msg}"))
            continue
        response = server.handle(message)
        if response is not None:
            _write(stdout, response)
    return 0


def _write(stdout: TextIO, message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()

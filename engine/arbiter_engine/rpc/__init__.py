"""Line-delimited JSON-RPC chassis for the M0 engine stub."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from arbiter_engine import __version__


def main() -> int:
    serve(sys.stdin, sys.stdout)
    return 0


def serve(stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        if not line.strip():
            continue
        response = _dispatch_line(line)
        stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        stdout.flush()


def _dispatch_line(line: str) -> dict[str, Any]:
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        return _error(None, -32700, "parse error", {"kind": "invalid_json", "detail": str(exc)})
    if not isinstance(request, dict):
        return _error(None, -32600, "invalid request", {"kind": "invalid_request"})

    request_id = request.get("id")
    if request.get("jsonrpc") != "2.0":
        return _error(request_id, -32600, "invalid request", {"kind": "invalid_jsonrpc"})

    method = request.get("method")
    if method == "initialize":
        return _result(
            request_id,
            {
                "engine": "arbiter-engine",
                "version": __version__,
                "capabilities": {"tools": True},
            },
        )
    if method == "tools/call":
        return _handle_tools_call(request_id, request.get("params", {}))
    return _error(request_id, -32601, "method not found", {"kind": "method_not_found"})


def _handle_tools_call(request_id: Any, params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return _error(request_id, -32602, "invalid params", {"kind": "invalid_params"})
    if params.get("name") != "ping":
        return _error(request_id, -32601, "tool not found", {"kind": "tool_not_found"})

    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        return _error(request_id, -32602, "invalid arguments", {"kind": "invalid_params"})
    message = arguments.get("message", "")
    return _result(
        request_id,
        {
            "content": [{"type": "text", "text": f"pong: {message}"}],
            "isError": False,
        },
    )


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": data},
    }

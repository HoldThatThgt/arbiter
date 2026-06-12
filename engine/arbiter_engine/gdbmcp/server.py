from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, Iterable, Mapping, Optional, TextIO

from . import __version__
from .audit import AuditLog
from .config import Config
from .errors import ProtocolError, ToolError, as_tool_error
from .sessions import SessionManager
from .tools import ToolRegistry, summarize_call, text_summary


JSON = Dict[str, Any]


class MCPServer:
    def __init__(self, config: Config):
        self.config = config
        self.audit = AuditLog(config)
        self.manager = SessionManager(config, self.audit)
        self.registry = ToolRegistry(self.manager, config)
        self.should_exit = False

    def handle(self, request: Any) -> Optional[JSON]:
        if isinstance(request, list):
            responses = [response for response in (self.handle(item) for item in request) if response is not None]
            return responses  # type: ignore[return-value]
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict):
                raise ProtocolError(-32600, "invalid request", {"kind": "request_not_object"})
            if request.get("jsonrpc") != "2.0":
                raise ProtocolError(-32600, "invalid request", {"kind": "invalid_jsonrpc"})
            method = request.get("method")
            if not isinstance(method, str):
                raise ProtocolError(-32600, "invalid request", {"kind": "missing_method"})
            if "id" not in request:
                self._handle_notification(method, request.get("params"))
                return None
            return self._result(request_id, self._dispatch(method, request.get("params")))
        except ProtocolError as exc:
            return self._error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            return self._error(request_id, -32603, "internal error", {"kind": "internal_error", "message": str(exc)})

    def _dispatch(self, method: str, params: Any) -> Any:
        if method == "initialize":
            protocol = "2025-06-18"
            if isinstance(params, dict) and isinstance(params.get("protocolVersion"), str):
                protocol = params["protocolVersion"]
            return {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "gdb-mcp", "version": __version__},
                "instructions": "Use structured GDB tools first; use gdb_command only for bounded console fallbacks.",
            }
        if method == "ping":
            return {}
        if method == "shutdown":
            self.should_exit = True
            return {}
        if method == "tools/list":
            return {"tools": self.registry.descriptors()}
        if method == "tools/call":
            return self._call_tool(params)
        raise ProtocolError(-32601, "method not found", {"method": method})

    def _handle_notification(self, method: str, params: Any) -> None:
        if method == "notifications/initialized":
            return
        if method == "exit":
            self.should_exit = True
            return
        return

    def _call_tool(self, params: Any) -> JSON:
        if not isinstance(params, dict):
            raise ProtocolError(-32602, "invalid params", {"kind": "params_not_object"})
        name = params.get("name")
        if not isinstance(name, str):
            raise ProtocolError(-32602, "invalid params", {"kind": "missing_tool_name"})
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProtocolError(-32602, "invalid params", {"kind": "arguments_not_object"})
        try:
            schema = self.registry.schema(name)
        except ToolError as exc:
            raise ProtocolError(-32601, "tool not found", exc.to_payload()["error"])
        _validate_args(schema, arguments)
        started = time.time()
        session_id = arguments.get("session_id") if isinstance(arguments.get("session_id"), str) else None
        self.audit.record("started", tool=name, session_id=session_id, summary=summarize_call(name, arguments))
        try:
            structured = self.registry.call(name, arguments)
            elapsed_ms = int((time.time() - started) * 1000)
            sid = structured.get("session_id") if isinstance(structured.get("session_id"), str) else session_id
            self.audit.record(
                "finished",
                tool=name,
                session_id=sid,
                ok=True,
                elapsed_ms=elapsed_ms,
                summary=summarize_call(name, arguments, structured),
            )
            return _tool_result(structured, is_error=False)
        except Exception as exc:
            tool_error = as_tool_error(exc)
            elapsed_ms = int((time.time() - started) * 1000)
            self.audit.record(
                "error",
                tool=name,
                session_id=session_id,
                ok=False,
                elapsed_ms=elapsed_ms,
                summary={"code": tool_error.code, **summarize_call(name, arguments)},
            )
            return _tool_result(tool_error.to_payload(), is_error=True)

    def _result(self, request_id: Any, result: Any) -> JSON:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str, data: Optional[Mapping[str, Any]] = None) -> JSON:
        error: JSON = {"code": code, "message": message}
        if data:
            error["data"] = dict(data)
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    def close(self) -> None:
        self.manager.close_all()


def serve(config: Config, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    server = MCPServer(config)
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error", "data": {"message": str(exc)}}}
            else:
                response = server.handle(request)
            if response is not None:
                stdout.write(json.dumps(response, separators=(",", ":"), sort_keys=True) + "\n")
                stdout.flush()
            if server.should_exit:
                break
    finally:
        server.close()
    return 0


def _tool_result(structured: Mapping[str, Any], *, is_error: bool) -> JSON:
    text = text_summary(structured)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": dict(structured),
        "isError": bool(is_error),
    }


def _validate_args(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    if schema.get("type") != "object":
        raise ProtocolError(-32603, "internal schema error", {"kind": "schema_not_object"})
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ProtocolError(-32603, "internal schema error", {"kind": "schema_properties_invalid"})
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise ProtocolError(-32603, "internal schema error", {"kind": "schema_required_invalid"})
    for key in required:
        if key not in arguments:
            raise ProtocolError(-32602, "invalid arguments", {"kind": "missing_required", "field": key})
    additional = schema.get("additionalProperties", True)
    if additional is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ProtocolError(-32602, "invalid arguments", {"kind": "unknown_arguments", "fields": unknown})
    for key, value in arguments.items():
        subschema = properties.get(key)
        if isinstance(subschema, dict):
            _validate_value(key, value, subschema)


def _validate_value(name: str, value: Any, schema: Mapping[str, Any]) -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise ProtocolError(-32602, "invalid arguments", {"kind": "bad_enum", "field": name, "allowed": list(schema["enum"])})
    expected = schema.get("type")
    if expected == "string" and not isinstance(value, str):
        raise ProtocolError(-32602, "invalid arguments", {"kind": "bad_type", "field": name, "expected": expected})
    if expected == "boolean" and not isinstance(value, bool):
        raise ProtocolError(-32602, "invalid arguments", {"kind": "bad_type", "field": name, "expected": expected})
    if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ProtocolError(-32602, "invalid arguments", {"kind": "bad_type", "field": name, "expected": expected})
    if expected == "array":
        if not isinstance(value, list):
            raise ProtocolError(-32602, "invalid arguments", {"kind": "bad_type", "field": name, "expected": expected})
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                _validate_value(f"{name}[{idx}]", item, item_schema)
    if expected == "object":
        if not isinstance(value, dict):
            raise ProtocolError(-32602, "invalid arguments", {"kind": "bad_type", "field": name, "expected": expected})
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise ProtocolError(-32602, "invalid arguments", {"kind": "too_small", "field": name, "minimum": minimum})
        if maximum is not None and value > maximum:
            raise ProtocolError(-32602, "invalid arguments", {"kind": "too_large", "field": name, "maximum": maximum})


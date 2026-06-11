"""Line-delimited JSON-RPC chassis for the Arbiter engine."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, TextIO

from arbiter_engine import __version__
from arbiter_engine.errors import RPCError, engine_stale


MAX_LINE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Context:
    meta: Mapping[str, Any]
    role: str


@dataclass(frozen=True)
class Tool:
    namespace: str
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[[Context, Mapping[str, Any]], Mapping[str, Any]]

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
        }


class Router:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def tool_descriptors(self) -> list[dict[str, Any]]:
        return [self._tools[name].descriptor() for name in sorted(self._tools)]

    def call_tool(
        self, name: str, arguments: Mapping[str, Any], context: Context
    ) -> Mapping[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise RPCError(-32601, "tool not found", {"kind": "tool_not_found"})
        _validate_args(tool.input_schema, arguments)
        return tool.handler(context, arguments)


def main() -> int:
    serve(sys.stdin, sys.stdout)
    return 0


def serve(stdin: TextIO, stdout: TextIO, router: Optional[Router] = None) -> None:
    active_router = router or default_router()
    for line in stdin:
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            response = _error(
                None,
                -32600,
                "invalid request",
                {"kind": "line_too_large", "limit": MAX_LINE_BYTES},
            )
        elif not line.strip():
            continue
        else:
            response = _dispatch_line(line, active_router)
        stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        stdout.flush()


def default_router() -> Router:
    router = Router()
    for namespace, name, description, schema in _DEFAULT_TOOLS:
        router.register(Tool(namespace, name, description, schema, _stub_handler(namespace, name)))
    return router


def _dispatch_line(line: str, router: Router) -> dict[str, Any]:
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        return _error(None, -32700, "parse error", {"kind": "invalid_json", "detail": str(exc)})

    try:
        return _dispatch(request, router)
    except RPCError as exc:
        request_id = request.get("id") if isinstance(request, dict) else None
        return _error(request_id, exc.code, exc.message, exc.data)


def _dispatch(request: Any, router: Router) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise RPCError(-32600, "invalid request", {"kind": "invalid_request"})

    request_id = request.get("id")
    if request.get("jsonrpc") != "2.0":
        raise RPCError(-32600, "invalid request", {"kind": "invalid_jsonrpc"})
    method = request.get("method")
    if not isinstance(method, str):
        raise RPCError(-32600, "invalid request", {"kind": "invalid_method"})

    if method == "initialize":
        return _result(
            request_id,
            {
                "engine": "arbiter-engine",
                "version": __version__,
                "capabilities": {"tools": True},
            },
        )
    if method == "tools/list":
        _expect_params_object(request.get("params", {}), allowed=())
        return _result(request_id, {"tools": router.tool_descriptors()})
    if method == "tools/call":
        return _handle_tools_call(request_id, request.get("params", {}), router)
    if method == "arbiter/handshake":
        return _handle_handshake(request_id, request.get("params", {}))

    raise RPCError(-32601, "method not found", {"kind": "method_not_found"})


def _handle_tools_call(request_id: Any, params: Any, router: Router) -> dict[str, Any]:
    values = _expect_params_object(params, allowed=("name", "arguments", "_meta"))
    name = values.get("name")
    if not isinstance(name, str):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "name"})

    arguments = values.get("arguments", {})
    if not isinstance(arguments, dict):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args"})

    meta = values.get("_meta", {})
    if not isinstance(meta, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_meta"})

    context = Context(meta=meta, role=os.environ.get("ARBITER_ENGINE_ROLE", "QUERY"))
    return _result(request_id, dict(router.call_tool(name, arguments, context)))


def _handle_handshake(request_id: Any, params: Any) -> dict[str, Any]:
    values = _expect_params_object(params, allowed=("expected_version",))
    expected = values.get("expected_version")
    if expected is not None and not isinstance(expected, str):
        raise RPCError(
            -32602,
            "invalid params",
            {"kind": "invalid_params", "field": "expected_version"},
        )
    if expected is not None and expected != __version__:
        raise engine_stale(expected, __version__)
    return _result(request_id, {"engine": "arbiter-engine", "version": __version__})


def _expect_params_object(params: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params"})
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise RPCError(
            -32602,
            "invalid params",
            {"kind": "invalid_params", "bad_params": unknown},
        )
    return dict(params)


def _validate_args(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise RPCError(-32603, "internal error", {"kind": "schema_invalid"})

    unknown = sorted(set(arguments) - set(properties))
    if unknown and schema.get("additionalProperties") is False:
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "bad_args": unknown})

    missing = sorted(name for name in required if name not in arguments)
    if missing:
        raise RPCError(
            -32602,
            "invalid arguments",
            {"kind": "invalid_args", "missing_args": missing},
        )

    for name, value in arguments.items():
        if name in properties:
            _validate_value(name, value, properties[name])


def _validate_value(name: str, value: Any, schema: Mapping[str, Any]) -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise RPCError(
            -32602,
            "invalid arguments",
            {"kind": "invalid_args", "field": name, "expected": "enum"},
        )

    expected = schema.get("type")
    if expected is None:
        return
    if not _matches_type(value, expected):
        raise RPCError(
            -32602,
            "invalid arguments",
            {"kind": "invalid_args", "field": name, "expected": expected},
        )

    if expected == "array" and "items" in schema:
        for item in value:
            _validate_value(name, item, schema["items"])


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _stub_handler(namespace: str, name: str) -> Callable[[Context, Mapping[str, Any]], Mapping[str, Any]]:
    def handler(context: Context, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "content": [{"type": "text", "text": f"{namespace}.{name} stub"}],
            "isError": False,
            "namespace": namespace,
            "tool": name,
        }

    return handler


def _result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _error(request_id: Any, code: int, message: str, data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": dict(data)},
    }


def _object_schema(properties: Mapping[str, Mapping[str, Any]], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: dict(schema) for name, schema in properties.items()},
        "required": list(required),
        "additionalProperties": False,
    }


_BUDGET = {"type": "string", "enum": ["small", "normal", "large"]}

_DEFAULT_TOOLS = (
    (
        "facts",
        "search",
        "Search the fact index.",
        _object_schema({"query": {"type": "string"}, "budget": _BUDGET}, ("query",)),
    ),
    (
        "facts",
        "detail",
        "Fetch fact detail by object id.",
        _object_schema({"id": {"type": "string"}, "budget": _BUDGET}, ("id",)),
    ),
    (
        "runs",
        "run",
        "Run a recipe-backed test target.",
        _object_schema(
            {
                "recipe": {"type": "string"},
                "tests": {"type": "array", "items": {"type": "string"}},
                "options": {"type": "object"},
            },
            ("recipe",),
        ),
    ),
    (
        "runs",
        "recipe_search",
        "Search registered run recipes.",
        _object_schema({"query": {"type": "string"}}, ("query",)),
    ),
    (
        "runs",
        "register",
        "Register a recipe book.",
        _object_schema({"path": {"type": "string"}}, ("path",)),
    ),
    (
        "runs",
        "import_recipes",
        "Import recipes from a path.",
        _object_schema({"path": {"type": "string"}}, ("path",)),
    ),
    (
        "runs",
        "scan",
        "Scan for test targets.",
        _object_schema({"scope": {"type": "string"}}, ("scope",)),
    ),
)

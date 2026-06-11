"""Line-delimited JSON-RPC chassis for the Arbiter engine."""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TextIO

from arbiter_engine import __version__
from arbiter_engine.errors import RPCError, briefing_unresolved, engine_stale
from arbiter_engine.facts import descriptors as facts_descriptors
from arbiter_engine.facts import view as facts_view
from arbiter_engine.runs import RunManager
from arbiter_engine.runs import async_runs
from arbiter_engine.runs import gtest
from arbiter_engine.runs import recipes
from arbiter_engine.shared import census


MAX_LINE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Context:
    meta: Mapping[str, Any]
    role: str
    seat: str


@dataclass(frozen=True)
class Tool:
    namespace: str
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[[Context, Mapping[str, Any]], Mapping[str, Any]]
    title: Optional[str] = None
    output_schema: Optional[Mapping[str, Any]] = None

    def descriptor(self) -> dict[str, Any]:
        row = {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
        }
        if self.title is not None:
            row = {
                "name": self.name,
                "title": self.title,
                "description": self.description,
                "inputSchema": dict(self.input_schema),
            }
        if self.output_schema is not None:
            row["outputSchema"] = dict(self.output_schema)
        return row


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
    for descriptor in facts_descriptors.tool_descriptors():
        name = descriptor["name"]
        router.register(
            Tool(
                namespace="facts",
                name=name,
                description=descriptor["description"],
                input_schema=descriptor["inputSchema"],
                handler=_handler("facts", name),
                title=descriptor.get("title"),
                output_schema=descriptor.get("outputSchema"),
            )
        )
    for namespace, name, description, schema in _DEFAULT_TOOLS:
        router.register(Tool(namespace, name, description, schema, _handler(namespace, name)))
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
    if method == "arbiter/refresh":
        return _handle_refresh(request_id, request.get("params", {}))
    if method == "arbiter/census":
        return _handle_census(request_id, request.get("params", {}))
    if method == "arbiter/resolveBriefing":
        return _handle_resolve_briefing(request_id, request.get("params", {}))
    if method == "arbiter/startRun":
        return _handle_start_run(request_id, request.get("params", {}))
    if method == "arbiter/runStatus":
        return _handle_run_status(request_id, request.get("params", {}))

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

    context = _context(meta)
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


def _handle_start_run(request_id: Any, params: Any) -> dict[str, Any]:
    values = _expect_params_object(
        params,
        allowed=("duration_ms", "timeout_ms", "overall", "_meta"),
    )
    meta = values.pop("_meta", {})
    if not isinstance(meta, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_meta"})
    try:
        result = RunManager(Path(os.getcwd())).start_run(values, meta=meta)
    except ValueError as exc:
        raise RPCError(
            -32602,
            "invalid params",
            {"kind": "invalid_params", "detail": str(exc)},
        ) from exc
    return _result(request_id, result)


def _handle_refresh(request_id: Any, params: Any) -> dict[str, Any]:
    values = _expect_params_object(params, allowed=("scope", "_meta"))
    scope = values.get("scope", {})
    meta = values.get("_meta", {})
    if not isinstance(scope, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "scope"})
    if not isinstance(meta, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_meta"})
    view = facts_view.refresh(Path.cwd(), _facts_context(_context(meta)))
    return _result(request_id, {"refreshed": True, "scope": dict(scope), **view.evidence()})


def _handle_census(request_id: Any, params: Any) -> dict[str, Any]:
    values = _expect_params_object(params, allowed=("scope", "_meta"))
    scope = values.get("scope", {})
    meta = values.get("_meta", {})
    if not isinstance(scope, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "scope"})
    if not isinstance(meta, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_meta"})
    bad_scope = sorted(set(scope) - {"globs", "previous"})
    if bad_scope:
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "bad_scope": bad_scope})
    globs = scope.get("globs", ["**/*"])
    if not isinstance(globs, list) or not all(isinstance(item, str) for item in globs):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "globs"})
    previous = None
    if "previous" in scope:
        raw_previous = scope["previous"]
        if not isinstance(raw_previous, dict):
            raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "previous"})
        try:
            previous = census.from_json(raw_previous)
        except ValueError as exc:
            raise RPCError(
                -32602,
                "invalid params",
                {"kind": "invalid_params", "field": "previous", "detail": str(exc)},
            )
    return _result(request_id, census.to_json(census.scan(Path.cwd(), globs, previous=previous)))


def _handle_resolve_briefing(request_id: Any, params: Any) -> dict[str, Any]:
    values = _expect_params_object(params, allowed=("refs", "_meta"))
    refs = values.get("refs")
    meta = values.get("_meta", {})
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "refs"})
    if len(refs) > 8:
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "refs"})
    if not isinstance(meta, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_meta"})
    bad_refs = [ref for ref in refs if ref.startswith("bad:")]
    if bad_refs:
        raise briefing_unresolved(bad_refs)
    return _result(
        request_id,
        {"briefing": [{"ref": ref, "content": f"detail {ref}"} for ref in refs]},
    )


def _handle_run_status(request_id: Any, params: Any) -> dict[str, Any]:
    values = _expect_params_object(params, allowed=("run_id",))
    run_id = values.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "run_id"})
    try:
        result = RunManager(Path(os.getcwd())).run_status(run_id)
    except KeyError as exc:
        raise RPCError(
            -32602,
            "invalid params",
            {"kind": "invalid_params", "field": "run_id"},
        ) from exc
    return _result(request_id, result)


def _context(meta: Mapping[str, Any]) -> Context:
    return Context(
        meta=meta,
        role=os.environ.get("ARBITER_ENGINE_ROLE", "QUERY"),
        seat=os.environ.get("ARBITER_ENGINE_SEAT", "player"),
    )


def _facts_context(context: Context) -> facts_view.AccessContext:
    return facts_view.AccessContext(role=context.role, seat=context.seat)


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
    if expected == "integer":
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int) and value < minimum:
            raise RPCError(
                -32602,
                "invalid arguments",
                {"kind": "invalid_args", "field": name, "minimum": minimum},
            )
        if isinstance(maximum, int) and value > maximum:
            raise RPCError(
                -32602,
                "invalid arguments",
                {"kind": "invalid_args", "field": name, "maximum": maximum},
            )
    if expected == "string":
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise RPCError(
                -32602,
                "invalid arguments",
                {"kind": "invalid_args", "field": name, "minLength": min_length},
            )


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


def _handler(namespace: str, name: str) -> Callable[[Context, Mapping[str, Any]], Mapping[str, Any]]:
    if namespace == "facts":
        if name == "search":
            return _facts_search_tool
        if name == "detail":
            return _facts_detail_tool
    if namespace == "runs":
        if name == "run":
            return _run_tool
        if name == "recipe_search":
            return _recipe_search_tool
        if name == "register":
            return _register_tool
        if name == "import_recipes":
            return _import_recipes_tool
    return _stub_handler(namespace, name)


def _facts_search_tool(context: Context, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    view = facts_view.access(Path.cwd(), _facts_context(context))
    query = arguments.get("query")
    if not isinstance(query, str):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "query"})
    limit = arguments.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "limit"})
    query_kind = _query_kind(query)
    structured = {
        **view.evidence(),
        "status": "ok",
        "query_kind": query_kind,
        "query": query,
        "limit": limit,
        "result_count": 0,
        "truncated": False,
        "results": [],
    }
    return {
        "content": [{"type": "text", "text": _search_text(structured)}],
        "structuredContent": structured,
        "isError": False,
    }


def _facts_detail_tool(context: Context, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    facts_view.access(Path.cwd(), _facts_context(context))
    fact_id = arguments.get("fact_id")
    if not isinstance(fact_id, str) or not fact_id:
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "fact_id"})
    message = (
        f"FACT id not found: {fact_id}.\n"
        "This id is not in the current snapshot; it may be stale or mistyped.\n"
        "Re-run search('<symbol name>') to obtain a valid object_id."
    )
    return {
        "content": [{"type": "text", "text": "not_found: " + message}],
        "structuredContent": {
            "error": {
                "code": "not_found",
                "message": message,
                "details": {"fact_id": fact_id},
            }
        },
        "isError": True,
    }


def _query_kind(query: str) -> str:
    if not query.strip():
        return "empty"
    if query.startswith("reachable:"):
        return "relation_reachable"
    if "depth:" in query:
        return "relation_transitive"
    if ":" in query:
        return "relation"
    return "terms"


def _search_text(structured: Mapping[str, Any]) -> str:
    snapshot = structured.get("base_snapshot_id") or "none"
    return (
        f"snapshot {snapshot} view_state={structured.get('view_state')}: "
        f"search returned {structured.get('result_count')} fact results "
        f"for query kind {structured.get('query_kind')}"
    )


def _run_tool(context: Context, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    del context
    recipe_id = arguments.get("recipe")
    if not isinstance(recipe_id, str):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "recipe"})
    tests = arguments.get("tests", [])
    if not isinstance(tests, list) or not all(isinstance(item, str) for item in tests):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "tests"})
    options = arguments.get("options", {})
    if not isinstance(options, dict):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "options"})
    profiles = _validate_run_options(options)
    book = _load_committed_recipe_book()
    result = gtest.run_target(Path.cwd(), book, recipe_id, run_id=uuid.uuid4().hex, tests=tests, profiles=profiles)
    payload = result.to_json()
    payload["isError"] = False
    payload["content"] = [{"type": "text", "text": f"{recipe_id}: {result.overall}"}]
    return payload


def _recipe_search_tool(context: Context, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    del context
    query = arguments.get("query")
    if not isinstance(query, str):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "query"})
    try:
        book = _load_committed_recipe_book()
    except RPCError:
        matches = []
    else:
        folded = query.lower()
        matches = [
            {
                "id": target.id,
                "harness": target.harness.kind,
                "notes": target.notes or "",
                "tests": list(target.tests),
            }
            for target in book.targets
            if folded in target.id.lower()
            or folded in (target.notes or "").lower()
            or any(folded in test.lower() for test in target.tests)
        ]
    return {
        "content": [{"type": "text", "text": f"{len(matches)} recipe matches"}],
        "isError": False,
        "matches": matches,
    }


def _register_tool(context: Context, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    del context
    book = _load_recipe_book_arg(arguments)
    return _recipe_book_summary(book, "registered")


def _import_recipes_tool(context: Context, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    del context
    book = _load_recipe_book_arg(arguments)
    return _recipe_book_summary(book, "imported")


def _recipe_book_summary(book: recipes.RecipeBook, verb: str) -> Mapping[str, Any]:
    targets = [target.id for target in book.targets]
    return {
        "content": [{"type": "text", "text": f"{verb} {len(targets)} recipes"}],
        "isError": False,
        "targets": targets,
        "profiles": sorted(book.profiles),
    }


def _load_recipe_book_arg(arguments: Mapping[str, Any]) -> recipes.RecipeBook:
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "path"})
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return recipes.load(path)
    except (OSError, recipes.RecipeError) as exc:
        raise RPCError(
            -32602,
            "invalid arguments",
            {"kind": "invalid_args", "field": "path", "detail": str(exc)},
        ) from exc


def _load_committed_recipe_book() -> recipes.RecipeBook:
    path = Path.cwd() / ".arbiter" / "recipes.yaml"
    try:
        return recipes.load(path)
    except (OSError, recipes.RecipeError) as exc:
        raise RPCError(
            -32602,
            "invalid arguments",
            {"kind": "invalid_args", "field": "recipe", "detail": str(exc)},
        ) from exc


def _validate_run_options(options: Mapping[str, Any]) -> tuple[str, ...]:
    allowed = {"profiles", "harness_options", "force_recompile"}
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "bad_options": unknown})
    profiles = options.get("profiles", [])
    if not isinstance(profiles, list) or not all(isinstance(item, str) for item in profiles):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "options.profiles"})
    harness_options = options.get("harness_options", {})
    if not isinstance(harness_options, dict):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "options.harness_options"})
    unknown_harnesses = sorted(set(harness_options) - {"gtest"})
    if unknown_harnesses:
        raise RPCError(
            -32602,
            "invalid arguments",
            {"kind": "invalid_args", "bad_harness_options": unknown_harnesses},
        )
    gtest_options = harness_options.get("gtest", {})
    if not isinstance(gtest_options, dict):
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "field": "options.harness_options.gtest"})
    unknown_gtest = sorted(set(gtest_options) - {"fail_fast", "timeout_s"})
    if unknown_gtest:
        raise RPCError(-32602, "invalid arguments", {"kind": "invalid_args", "bad_gtest_options": unknown_gtest})
    return tuple(profiles)


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


_DEFAULT_TOOLS = (
    (
        "runs",
        "run",
        "Run a recipe-backed test target.",
        _object_schema(
            {
                "recipe": {"type": "string"},
                "tests": {"type": "array", "items": {"type": "string"}},
                "options": {
                    "type": "object",
                    "properties": {
                        "profiles": {"type": "array", "items": {"type": "string"}},
                        "force_recompile": {"type": "boolean"},
                        "harness_options": {
                            "type": "object",
                            "properties": {
                                "gtest": {
                                    "type": "object",
                                    "properties": {
                                        "fail_fast": {"type": "boolean"},
                                        "timeout_s": {"type": "integer"},
                                    },
                                    "additionalProperties": False,
                                }
                            },
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                },
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

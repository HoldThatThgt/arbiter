"""cipher-2-MCP-shaped adapter over arbiter's rpc tool handlers (M4 test support).

Lets migrated mcp acceptance tests keep `open_facts_server(repo).search(...)/.detail(...)/
.call_tool(...)`. Binds to the repo via cwd + a QUERY/player Context and drives the real
`rpc.serve` loop over StringIO. Not a test module (no `test_` prefix), so unittest discover
ignores it.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest import mock

from arbiter_engine import rpc
from arbiter_engine.errors import RPCError


@contextmanager
def working_dir(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


@contextmanager
def engine_env(role="QUERY", seat="player"):
    with mock.patch.dict(os.environ, {"ARBITER_ENGINE_ROLE": role, "ARBITER_ENGINE_SEAT": seat}):
        yield


@dataclass(frozen=True)
class ToolCallResult:
    structured_content: Optional[Mapping[str, Any]]
    content: list
    is_error: bool


def _wrap(value):
    if isinstance(value, dict):
        return _AttrView(value)
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


class _AttrView:
    """Recursive attribute/index view over a JSON dict so migrated tests can write
    response.results[0].object_id / response.payload_preview["owner_name"] / response.anchor."""

    def __init__(self, data):
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name):
        data = object.__getattribute__(self, "_data")
        if isinstance(data, dict) and name in data:
            return _wrap(data[name])
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        # cipher-2 responses are dataclasses: list fields default to [], other optional
        # fields to None. to_json() omits empties, so restore the dataclass default here.
        if name in getattr(type(self), "_LIST_FIELDS", frozenset()):
            return []
        return None

    def __getitem__(self, key):
        return _wrap(self._data[key])

    def __contains__(self, key):
        return key in self._data

    def get(self, key, default=None):
        if key in self._data:
            return _wrap(self._data[key])
        return default

    def __eq__(self, other):
        return self._data == (other._data if isinstance(other, _AttrView) else other)

    def __iter__(self):
        return iter(_wrap(v) for v in self._data)

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        return f"_AttrView({self._data!r})"


class _SearchResponse(_AttrView):
    """structuredContent of a successful search call as an attribute/index view."""

    _LIST_FIELDS = frozenset(
        {"results", "anchor_candidates", "available_filters", "examples", "top_by_salience", "path"}
    )


class _DetailResponse(_AttrView):
    """structuredContent of a successful detail call as an attribute/index view."""


class _FactsServer:
    def __init__(self, repo, role="QUERY", seat="player", *, fact_view_provider=None, log_enabled=True):
        self.target_repo = Path(repo)
        self.log_enabled = log_enabled
        self._role, self._seat = role, seat
        self._fact_view_provider = fact_view_provider  # accepted for signature-compat; unused

    def call_tool(self, name: str, args: Mapping[str, Any]) -> ToolCallResult:
        line = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": dict(args)}},
            separators=(",", ":"),
        ) + "\n"
        with working_dir(self.target_repo), engine_env(self._role, self._seat):
            out = io.StringIO()
            rpc.serve(io.StringIO(line), out)
            resp = json.loads(out.getvalue())
        if "error" in resp:  # protocol-level error -> RPCError (the chassis plane)
            err = resp["error"]
            raise RPCError(err["code"], err["message"], err.get("data"))
        result = resp["result"]
        return ToolCallResult(
            structured_content=result.get("structuredContent"),
            content=result.get("content", []),
            is_error=bool(result.get("isError", False)),
        )

    def search(self, query: str, limit: int = 20) -> _SearchResponse:
        return _SearchResponse(self.call_tool("search", {"query": query, "limit": limit}).structured_content)

    def detail(self, fact_id: str, budget: str = "normal") -> _DetailResponse:
        return _DetailResponse(self.call_tool("detail", {"fact_id": fact_id, "budget": budget}).structured_content)


def open_facts_server(repo, *, role="QUERY", seat="player", fact_view_provider=None, log_enabled=True) -> _FactsServer:
    return _FactsServer(Path(repo), role=role, seat=seat,
                        fact_view_provider=fact_view_provider, log_enabled=log_enabled)

"""Local stdio MCP server for FACT-only search and detail tools."""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, TextIO, Tuple

from cipher2 import __version__
from cipher2.common import JSONValue
from cipher2.config import ConfigError, load_config
from cipher2.incremental import IncrementalCoordinator, IncrementalError
from cipher2.storage import (
    FactRecord,
    FactRelative,
    RELATION_KINDS,
    RELATION_TRANSITIVE_FRONTIER_BUDGET,
    RELATION_TRANSITIVE_VISITED_BUDGET,
    RelationSearchAnchorCandidate,
    RelationSearchMatch,
    RelationSearchResult,
    StorageError,
    open_fact_store,
    parse_relation_search_query,
)
from cipher2.tools.log import LogError, LogEvent, open_log
from .descriptors import ToolDescriptor, _detail_descriptor, _search_descriptor


PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_METHODS = {"initialize", "notifications/initialized", "tools/list", "tools/call", "ping"}
BUDGETS = {
    "small": {"payload_fields": 16, "string_chars": 128, "source_radius": 3, "response_bytes": 8 * 1024},
    "normal": {"payload_fields": 32, "string_chars": 256, "source_radius": 10, "response_bytes": 32 * 1024},
    "large": {"payload_fields": 48, "string_chars": 384, "source_radius": 20, "response_bytes": 128 * 1024},
}
SEARCH_PREVIEW_FIELDS = 8
SEARCH_PREVIEW_CHARS = 128
MAX_QUERY_PREVIEW = 80
RELATIVE_PREVIEW_BUCKET_LIMITS = {"small": 5, "normal": 25, "large": 50}
RELATIVE_PREVIEW_FLAT_LIMIT = 8
RELATIVE_PREVIEW_FETCH_LIMIT = 100
RELATIVE_PREVIEW_SOURCE_SOFT_CAP = 2
RELATIVE_PREVIEW_ORDER = (
    ("incoming", "direct_call"),
    ("outgoing", "direct_call"),
    ("incoming", "field_read"),
    ("incoming", "field_write"),
    ("outgoing", "field_read"),
    ("outgoing", "field_write"),
    ("incoming", "has_field"),
    ("outgoing", "has_field"),
    ("incoming", "assigned_to"),
    ("outgoing", "assigned_to"),
    ("incoming", "dispatches_via"),
    ("outgoing", "dispatches_via"),
    ("incoming", "include"),
    ("outgoing", "include"),
    ("incoming", "defines"),
    ("outgoing", "defines"),
    ("incoming", "declares"),
    ("outgoing", "declares"),
)
RELATIVE_SALIENCE_RANKS = {
    "direct_call": 0,
    "dispatches_via": 0,
    "field_write": 1,
    "field_read": 2,
    "assigned_to": 3,
    "has_field": 4,
    "defines": 4,
    "include": 4,
}
EXACT_OBJECT_NAME_QUERY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class McpError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_json(self) -> Dict[str, JSONValue]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class ToolListResponse:
    tools: List[ToolDescriptor]

    def to_json(self) -> Dict[str, JSONValue]:
        return {"tools": [tool.to_json() for tool in self.tools]}


@dataclass(frozen=True)
class ToolCallResult:
    content: List[Dict[str, JSONValue]]
    structured_content: Dict[str, JSONValue]
    is_error: bool = False

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "content": self.content,
            "structuredContent": self.structured_content,
            "isError": self.is_error,
        }


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int = 20


@dataclass(frozen=True)
class FactSummary:
    object_id: str
    object_name: str
    object_description: str
    object_source: str
    object_profile: str
    object_caller: Optional[str]
    object_callee: Optional[str]
    payload_preview: Dict[str, JSONValue]
    truncated: bool
    matched_relations: List[Dict[str, JSONValue]] = field(default_factory=list)

    def to_json(self) -> Dict[str, JSONValue]:
        row: Dict[str, JSONValue] = {
            "object_id": self.object_id,
            "object_name": self.object_name,
            "object_description": self.object_description,
            "object_source": self.object_source,
            "object_profile": self.object_profile,
            "object_caller": self.object_caller,
            "object_callee": self.object_callee,
            "payload_preview": self.payload_preview,
            "truncated": self.truncated,
        }
        if self.matched_relations:
            row["matched_relations"] = [dict(relation) for relation in self.matched_relations]
        return row


@dataclass(frozen=True)
class RelationEndpointSummary:
    object_id: str
    object_name: str
    object_source: str
    relation_kind: str
    instances: int
    representative_relative_id: str
    hop: int

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "object_id": self.object_id,
            "object_name": self.object_name,
            "object_source": self.object_source,
            "relation_kind": self.relation_kind,
            "instances": self.instances,
            "representative_relative_id": self.representative_relative_id,
            "hop": self.hop,
        }


@dataclass(frozen=True)
class RelationPathSummary:
    object_id: str
    object_name: str
    object_source: str
    hop: int
    relation_kind: Optional[str] = None
    representative_relative_id: Optional[str] = None
    condition: Optional[Dict[str, JSONValue]] = None

    def to_json(self) -> Dict[str, JSONValue]:
        row: Dict[str, JSONValue] = {
            "object_id": self.object_id,
            "object_name": self.object_name,
            "object_source": self.object_source,
            "hop": self.hop,
        }
        if self.relation_kind is not None:
            row["relation_kind"] = self.relation_kind
        if self.representative_relative_id is not None:
            row["representative_relative_id"] = self.representative_relative_id
        if self.condition is not None:
            row["condition"] = dict(self.condition)
        return row


@dataclass(frozen=True)
class ViewStateMetadata:
    view_state: str
    base_snapshot_id: Optional[str]
    overlay_id: Optional[str]
    stale_source_count: int = 0
    pending_task_count: int = 0

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "view_state": self.view_state,
            "base_snapshot_id": self.base_snapshot_id,
            "overlay_id": self.overlay_id,
            "stale_source_count": self.stale_source_count,
            "pending_task_count": self.pending_task_count,
        }


@dataclass(frozen=True)
class SearchResponse:
    view_state: ViewStateMetadata
    query: str
    limit: int
    result_count: int
    truncated: bool
    results: List[object]
    status: str = "ok"
    query_kind: str = "terms"
    relation: Optional[str] = None
    anchor: Optional[FactSummary] = None
    total: Optional[int] = None
    message: Optional[str] = None
    available_filters: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)
    top_by_salience: List[object] = field(default_factory=list)
    anchor_candidates: List[FactSummary] = field(default_factory=list)
    matched_endpoint_count: Optional[int] = None
    complete: Optional[bool] = None
    budget_exhausted: Optional[bool] = None
    budget_exhausted_kind: Optional[str] = None
    total_is_exact: Optional[bool] = None
    reachable: Optional[bool] = None
    path: List[RelationPathSummary] = field(default_factory=list)
    depth_requested: Optional[int] = None
    depth_used: Optional[int] = None
    depth_max: Optional[int] = None
    visited_function_count: int = 0
    frontier_edge_count: int = 0
    skipped_missing_endpoint_count: int = 0

    def to_json(self) -> Dict[str, JSONValue]:
        row = self.view_state.to_json()
        row.update({
            "status": self.status,
            "query_kind": self.query_kind,
            "query": self.query,
            "limit": self.limit,
            "result_count": self.result_count,
            "truncated": self.truncated,
            "results": [summary.to_json() for summary in self.results],
        })
        if self.relation is not None:
            row["relation"] = self.relation
        if self.anchor is not None:
            row["anchor"] = self.anchor.to_json()
        if self.total is not None:
            row["total"] = self.total
        if self.message is not None:
            row["message"] = self.message
        if self.available_filters:
            row["available_filters"] = list(self.available_filters)
        if self.examples:
            row["examples"] = list(self.examples)
        if self.top_by_salience:
            row["top_by_salience"] = [summary.to_json() for summary in self.top_by_salience]
        if self.anchor_candidates:
            row["anchor_candidates"] = [summary.to_json() for summary in self.anchor_candidates]
        if self.matched_endpoint_count is not None:
            row["matched_endpoint_count"] = self.matched_endpoint_count
        if self.complete is not None:
            row["complete"] = self.complete
        if self.budget_exhausted is not None:
            row["budget_exhausted"] = self.budget_exhausted
        if self.budget_exhausted_kind is not None:
            row["budget_exhausted_kind"] = self.budget_exhausted_kind
        if self.total_is_exact is not None:
            row["total_is_exact"] = self.total_is_exact
        if self.reachable is not None:
            row["reachable"] = self.reachable
        if self.path:
            row["path"] = [node.to_json() for node in self.path]
        if self.depth_requested is not None:
            row["depth_requested"] = self.depth_requested
        if self.depth_used is not None:
            row["depth_used"] = self.depth_used
        if self.depth_max is not None:
            row["depth_max"] = self.depth_max
        return row


@dataclass(frozen=True)
class DetailRequest:
    fact_id: str
    budget: str = "normal"


@dataclass(frozen=True)
class SourceContext:
    source: str
    start_line: Optional[int]
    end_line: Optional[int]
    lines: List[str]
    truncated: bool
    unavailable_reason: Optional[str] = None

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "source": self.source,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "lines": list(self.lines),
            "truncated": self.truncated,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class DetailResponse:
    view_state: ViewStateMetadata
    fact: FactSummary
    payload: Dict[str, JSONValue]
    payload_truncated: bool
    source_context: Optional[SourceContext]
    relative_preview: "RelationPreview"
    response_bytes: int = 0
    response_bytes_limit: int = 0
    response_truncated: bool = False
    source_context_line_dropped_count: int = 0
    payload_field_dropped_count: int = 0

    def to_json(self) -> Dict[str, JSONValue]:
        row = self.view_state.to_json()
        row.update({
            "fact": self.fact.to_json(),
            "payload": self.payload,
            "payload_truncated": self.payload_truncated,
            "source_context": self.source_context.to_json() if self.source_context is not None else None,
            "relative_preview": self.relative_preview.to_json(),
        })
        return row


@dataclass(frozen=True)
class RelativeSummary:
    relative_id: str
    from_fact_id: str
    to_fact_id: str
    relation_kind: str
    condition: Optional[Dict[str, JSONValue]]
    evidence_source: str
    confidence: float
    payload_preview: Dict[str, JSONValue]
    truncated: bool
    conditions: List[Dict[str, JSONValue]] = field(default_factory=list)
    instances: int = 1
    endpoint_name: Optional[str] = None
    endpoint_profile: Optional[str] = None
    endpoint_source: Optional[str] = None

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "relative_id": self.relative_id,
            "from_fact_id": self.from_fact_id,
            "to_fact_id": self.to_fact_id,
            "relation_kind": self.relation_kind,
            "condition": dict(self.condition) if self.condition is not None else None,
            "evidence_source": self.evidence_source,
            "confidence": self.confidence,
            "payload_preview": self.payload_preview,
            "truncated": self.truncated,
            "conditions": [dict(condition) for condition in self.conditions],
            "instances": self.instances,
            "endpoint_name": self.endpoint_name,
            "endpoint_profile": self.endpoint_profile,
            "endpoint_source": self.endpoint_source,
        }


@dataclass(frozen=True)
class RelationPreviewBucket:
    bucket: str
    direction: str
    relation_kind: str
    total_count: int
    shown_count: int
    truncated: bool
    relatives: List[RelativeSummary]

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "bucket": self.bucket,
            "direction": self.direction,
            "relation_kind": self.relation_kind,
            "total_count": self.total_count,
            "shown_count": self.shown_count,
            "truncated": self.truncated,
            "relatives": [summary.to_json() for summary in self.relatives],
        }


@dataclass(frozen=True)
class RelationPreview:
    incoming_counts: Dict[str, int]
    outgoing_counts: Dict[str, int]
    relatives: List[RelativeSummary]
    buckets: List[RelationPreviewBucket]
    total_count: int
    shown_count: int
    truncated: bool
    rollup_group_count: int = 0
    collapsed_instance_count: int = 0
    source_file_count: int = 0
    diversity_bucket_count: int = 0
    flat_relative_count: int = 0
    flat_relative_dropped_count: int = 0
    bucket_relative_dropped_count: int = 0
    bucket_dropped_count: int = 0
    budget_exhausted: bool = False
    budget_exhausted_kind: Optional[str] = None

    def to_json(self) -> Dict[str, JSONValue]:
        row: Dict[str, JSONValue] = {
            "incoming_counts": dict(self.incoming_counts),
            "outgoing_counts": dict(self.outgoing_counts),
            "relatives": [summary.to_json() for summary in self.relatives],
            "buckets": [bucket.to_json() for bucket in self.buckets],
            "total_count": self.total_count,
            "shown_count": self.shown_count,
            "truncated": self.truncated,
        }
        if self.budget_exhausted:
            row["budget_exhausted"] = True
            row["budget_exhausted_kind"] = self.budget_exhausted_kind
        return row


@dataclass
class _RelativeRollup:
    key: Tuple[str, str, str]
    endpoint_id: str
    endpoint_name: str
    endpoint_profile: Optional[str]
    endpoint_source: Optional[str]
    endpoint_source_file: str
    endpoint_missing: bool
    direction: str
    relation_kind: str
    representative: FactRelative
    instances: int = 0
    has_unconditional: bool = False
    conditions: Dict[str, Dict[str, JSONValue]] = field(default_factory=dict)


class McpServer:
    def __init__(
        self,
        target_repo: Path,
        *,
        log_enabled: bool = True,
        fact_view_provider: Optional[Callable[[], object]] = None,
    ) -> None:
        target = Path(target_repo)
        if not target.exists() or not target.is_dir():
            raise McpError("invalid_target_repo", "target_repo must be a readable directory")
        if not isinstance(log_enabled, bool):
            raise McpError("invalid_log_enabled", "log_enabled must be a bool")
        if fact_view_provider is not None and not callable(fact_view_provider):
            raise McpError("invalid_fact_view_provider", "fact_view_provider must be callable")
        self.target_repo = target
        self.log_enabled = log_enabled
        self._fact_view_provider = fact_view_provider
        self._incremental_coordinator: Optional[IncrementalCoordinator] = None
        try:
            self.config = load_config(target, observe=False)
        except ConfigError as exc:
            raise McpError("invalid_config", exc.message, details={"config_code": exc.code}) from exc
        if self._fact_view_provider is None and self.config.incremental_temporary_enabled:
            try:
                coordinator = IncrementalCoordinator(target, self.config, log_enabled=log_enabled)
                coordinator.reconcile_current_sources()
                self._incremental_coordinator = coordinator
            except (IncrementalError, StorageError):
                self._incremental_coordinator = None

    def handle_message(self, message: object) -> Optional[Dict[str, JSONValue]]:
        if isinstance(message, list):
            return _json_rpc_error(None, "unsupported_batch", "JSON-RPC batch is not supported")
        if not isinstance(message, dict):
            return _json_rpc_error(None, "malformed_json", "JSON-RPC message must be an object")
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str) or method not in SUPPORTED_METHODS:
            return _json_rpc_error(request_id, "unknown_method", "unknown JSON-RPC method")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            started = time.perf_counter()
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cipher-2", "version": __version__},
            }
            self._emit_response(method, started=started)
            return _json_rpc_result(request_id, result)
        if method == "tools/list":
            self._emit_response(method, started=time.perf_counter())
            return _json_rpc_result(request_id, self.list_tools().to_json())
        if method == "ping":
            self._emit_response(method, started=time.perf_counter())
            return _json_rpc_result(request_id, {})
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _json_rpc_error(request_id, "invalid_args", "tools/call params must be an object")
        name = params.get("name")
        if name not in {"search", "detail"}:
            self._emit_error("unknown_tool", "unknown MCP tool", status="error", tool_name=str(name) if name is not None else None)
            return _json_rpc_error(request_id, "unknown_tool", "unknown MCP tool")
        arguments = params.get("arguments", {})
        result = self.call_tool(str(name), arguments)
        return _json_rpc_result(request_id, result.to_json())

    def list_tools(self) -> ToolListResponse:
        return ToolListResponse(tools=[_search_descriptor(), _detail_descriptor()])

    def call_tool(self, name: str, arguments: Dict[str, JSONValue]) -> ToolCallResult:
        started = time.perf_counter()
        if name not in {"search", "detail"}:
            error = McpError("unknown_tool", "unknown MCP tool", details={"tool_name": str(name)})
            self._emit_request(name, request_kind="tool_call", started=started)
            self._emit_error(error.code, error.message, status="error", tool_name=str(name), started=started)
            return _error_result(error)
        self._emit_request(name, request_kind="tool_call", started=started)
        if not isinstance(arguments, dict):
            error = McpError("invalid_args", "tools/call arguments must be an object")
            self._emit_error(error.code, error.message, status="error", tool_name=name, started=started)
            return _error_result(error)
        try:
            if name == "search":
                _reject_unknown_args(arguments, {"query", "limit"})
                query = arguments.get("query")
                limit = arguments.get("limit", 20)
                response = self.search(query, limit=limit)
                self._emit_search(response, started=started)
                return ToolCallResult(
                    content=[{"type": "text", "text": _search_text(response)}],
                    structured_content=response.to_json(),
                    is_error=False,
                )
            if name == "detail":
                _reject_unknown_args(arguments, {"fact_id", "budget"})
                fact_id = arguments.get("fact_id")
                budget = arguments.get("budget", "normal")
                response = self.detail(fact_id, budget=budget)
                self._emit_detail(response, budget=str(budget), started=started)
                return ToolCallResult(
                    content=[{"type": "text", "text": _detail_text(response)}],
                    structured_content=response.to_json(),
                    is_error=False,
                )
            raise McpError("unknown_tool", "unknown MCP tool", details={"tool_name": str(name)})
        except McpError as exc:
            self._emit_error(exc.code, exc.message, status="error", tool_name=name, started=started)
            return _error_result(exc)

    def search(self, query: object, limit: object = 20) -> SearchResponse:
        request = _validate_search_request(query, limit)
        view = self._current_view()
        try:
            relation_query = parse_relation_search_query(request.query)
        except StorageError as exc:
            raise McpError("invalid_query", exc.message, details={"storage_code": exc.code}) from exc
        if relation_query is not None:
            relation_search = getattr(view, "relation_search", None)
            if not callable(relation_search):
                raise McpError("storage_error", "relation search is not supported by this FACT view")
            try:
                relation_result = relation_search(request.query, limit=request.limit)
            except StorageError as exc:
                raise McpError("storage_error", exc.message, details={"storage_code": exc.code}) from exc
            return _search_response_from_relation_result(
                _view_state_from_view(view),
                request.query,
                request.limit,
                relation_result,
            )
        try:
            records = view.search(request.query, limit=request.limit + 1)
        except StorageError as exc:
            raise McpError("storage_error", exc.message, details={"storage_code": exc.code}) from exc
        truncated_by_limit = len(records) > request.limit
        records = records[: request.limit]
        results = [_summary_from_fact(record) for record in records]
        response = SearchResponse(
            view_state=_view_state_from_view(view),
            query=request.query,
            limit=request.limit,
            result_count=len(results),
            truncated=truncated_by_limit or any(result.truncated for result in results),
            results=results,
            query_kind="empty" if not _search_terms(request.query) else "terms",
            message=_plain_search_message(request.query, records),
        )
        return response

    def detail(self, fact_id: object, budget: object = "normal") -> DetailResponse:
        request = _validate_detail_request(fact_id, budget)
        view = self._current_view()
        try:
            record = view.get_fact(request.fact_id)
        except StorageError as exc:
            raise McpError("storage_error", exc.message, details={"storage_code": exc.code}) from exc
        if record is None:
            raise McpError(
                "not_found",
                (
                    f"FACT id not found: {request.fact_id}.\n"
                    "This id is not in the current snapshot; it may be stale or mistyped.\n"
                    "Re-run search('<symbol name>') to obtain a valid object_id."
                ),
                details={"fact_id": request.fact_id},
            )
        summary = _summary_from_fact(record)
        payload, payload_truncated = _bounded_payload(record.payload, request.budget)
        source_context = _source_context(self.target_repo, record.object_source, request.budget)
        relative_preview = self._relative_preview(view, record.object_id, request.budget)
        response = DetailResponse(
            view_state=_view_state_from_view(view),
            fact=summary,
            payload=payload,
            payload_truncated=payload_truncated,
            source_context=source_context,
            relative_preview=relative_preview,
        )
        return _enforce_detail_response_budget(response, request.budget)

    def _current_view(self):
        if self._fact_view_provider is not None:
            return self._fact_view_provider()
        if self._incremental_coordinator is not None:
            return self._incremental_coordinator.current_view()
        return open_fact_store(self.target_repo, mode="r", log_enabled=False).open_view(None)

    def _relative_preview(self, store, fact_id: str, budget: str) -> RelationPreview:
        bucket_limit = RELATIVE_PREVIEW_BUCKET_LIMITS[budget]
        incoming: Dict[str, int] = {}
        outgoing: Dict[str, int] = {}
        buckets: List[RelationPreviewBucket] = []
        total_count = 0
        truncated = False
        rollup_group_count = 0
        collapsed_instance_count = 0
        source_files = set()
        diversity_bucket_count = 0
        try:
            for direction, relation_kind in _relative_preview_keys():
                relatives = store.relatives_for_fact(
                    fact_id,
                    direction=direction,
                    relation_kind=relation_kind,
                    limit=RELATIVE_PREVIEW_FETCH_LIMIT,
                )
                if not relatives:
                    continue
                relation_total = _count_relatives_for_fact(store, fact_id, direction, relation_kind, relatives)
                (
                    summaries,
                    bucket_rollup_count,
                    bucket_collapsed_count,
                    bucket_source_files,
                    diversity_applied,
                ) = _select_relative_summaries(store, relatives, direction, relation_kind, bucket_limit)
                bucket_truncated = relation_total > bucket_limit
                counter = incoming if direction == "incoming" else outgoing
                counter[relation_kind] = relation_total
                total_count += relation_total
                rollup_group_count += bucket_rollup_count
                collapsed_instance_count += bucket_collapsed_count
                source_files.update(bucket_source_files)
                if diversity_applied:
                    diversity_bucket_count += 1
                truncated = truncated or bucket_truncated
                buckets.append(
                    RelationPreviewBucket(
                        bucket=_relative_bucket_name(direction, relation_kind),
                        direction=direction,
                        relation_kind=relation_kind,
                        total_count=relation_total,
                        shown_count=len(summaries),
                        truncated=bucket_truncated,
                        relatives=summaries,
                    )
                )
        except StorageError:
            return RelationPreview(
                incoming_counts={},
                outgoing_counts={},
                relatives=[],
                buckets=[],
                total_count=0,
                shown_count=0,
                truncated=False,
            )
        shown_count = _bucket_relative_count_from_buckets(buckets)
        flat = _relative_compat_sample(buckets, RELATIVE_PREVIEW_FLAT_LIMIT)
        return RelationPreview(
            incoming_counts=incoming,
            outgoing_counts=outgoing,
            relatives=flat,
            buckets=buckets,
            total_count=total_count,
            shown_count=shown_count,
            truncated=truncated or len(flat) < shown_count,
            rollup_group_count=rollup_group_count,
            collapsed_instance_count=collapsed_instance_count,
            source_file_count=len(source_files),
            diversity_bucket_count=diversity_bucket_count,
            flat_relative_count=len(flat),
            flat_relative_dropped_count=max(0, shown_count - len(flat)),
        )

    def _emit_request(self, tool_name: Optional[str], *, request_kind: str, started: float) -> None:
        self._write_event(
            LogEvent(
                event_name="mcp.request",
                channel="mcp",
                duration_ms=_elapsed_ms(started),
                counts={"request_count": 1},
                payload={
                    "operation": "tools/call",
                    "method": "tools/call",
                    "tool_name": tool_name,
                    "request_kind": request_kind,
                },
            )
        )

    def _emit_response(self, method: str, *, started: float) -> None:
        self._write_event(
            LogEvent(
                event_name="mcp.response",
                channel="mcp",
                duration_ms=_elapsed_ms(started),
                counts={"response_count": 1},
                payload={"operation": method, "method": method, "request_kind": "protocol"},
            )
        )

    def _emit_search(self, response: SearchResponse, *, started: float) -> None:
        terms = _search_terms(response.query)
        counts = {
            "result_count": response.result_count,
            "limit": response.limit,
            "term_count": len(terms),
            "truncated_count": 1 if response.truncated else 0,
        }
        if response.query_kind.startswith("relation"):
            counts.update(
                {
                    "anchor_candidate_count": len(response.anchor_candidates),
                    "matched_endpoint_count": response.matched_endpoint_count or 0,
                    "returned_count": response.result_count,
                    "too_broad_count": 1 if response.status == "too_broad" else 0,
                    "filter_count": _relation_filter_count(response),
                    "budget_exhausted_count": 1 if response.budget_exhausted else 0,
                    "depth_requested": response.depth_requested or 0,
                    "depth_used": response.depth_used or 0,
                    "depth_max": response.depth_max or 0,
                    "visited_function_count": response.visited_function_count,
                    "visited_function_budget": RELATION_TRANSITIVE_VISITED_BUDGET,
                    "frontier_edge_count": response.frontier_edge_count,
                    "frontier_edge_budget": RELATION_TRANSITIVE_FRONTIER_BUDGET,
                    "path_length": len(response.path),
                    "skipped_missing_endpoint_count": response.skipped_missing_endpoint_count,
                }
            )
        self._write_event(
            LogEvent(
                event_name="mcp.search",
                channel="mcp",
                duration_ms=_elapsed_ms(started),
                counts=counts,
                payload={
                    "operation": "search",
                    "outcome": response.status,
                    "query_kind": response.query_kind,
                    "query_preview": response.query[:MAX_QUERY_PREVIEW],
                    "returned_ids": _returned_ids(response),
                    "relation_predicate": response.relation,
                    "depth_requested": response.depth_requested,
                    "depth_used": response.depth_used,
                    "depth_max": response.depth_max,
                    "matched_endpoint_count": response.matched_endpoint_count,
                    "total_is_exact": response.total_is_exact,
                    "budget_exhausted": response.budget_exhausted,
                    "budget_exhausted_kind": response.budget_exhausted_kind,
                    "reachable_hit": response.reachable,
                    "visited_function_count": response.visited_function_count,
                    "visited_function_budget": RELATION_TRANSITIVE_VISITED_BUDGET,
                    "frontier_edge_count": response.frontier_edge_count,
                    "frontier_edge_budget": RELATION_TRANSITIVE_FRONTIER_BUDGET,
                    "path_length": len(response.path),
                    "skipped_missing_endpoint_count": response.skipped_missing_endpoint_count,
                    **response.view_state.to_json(),
                },
            )
        )

    def _emit_detail(self, response: DetailResponse, *, budget: str, started: float) -> None:
        unavailable = response.source_context.unavailable_reason if response.source_context is not None else None
        status = "warning" if unavailable else "ok"
        self._write_event(
            LogEvent(
                event_name="mcp.detail",
                channel="mcp",
                status=status,
                error_code=unavailable,
                duration_ms=_elapsed_ms(started),
                subject_id=response.fact.object_id,
                counts={
                    "response_bytes": response.response_bytes or _detail_response_json_bytes(response),
                    "response_bytes_limit": response.response_bytes_limit,
                    "response_truncated_count": 1 if response.response_truncated else 0,
                    "payload_field_count": len(response.payload),
                    "payload_field_dropped_count": response.payload_field_dropped_count,
                    "relative_count": response.relative_preview.shown_count,
                    "relative_total_count": response.relative_preview.total_count,
                    "relative_bucket_count": len(response.relative_preview.buckets),
                    "flat_relative_count": response.relative_preview.flat_relative_count,
                    "flat_relative_dropped_count": response.relative_preview.flat_relative_dropped_count,
                    "bucket_relative_dropped_count": response.relative_preview.bucket_relative_dropped_count,
                    "relative_bucket_dropped_count": response.relative_preview.bucket_dropped_count,
                    "budget_exhausted_count": 1 if response.relative_preview.budget_exhausted else 0,
                    "relative_rollup_group_count": response.relative_preview.rollup_group_count,
                    "relative_collapsed_instance_count": response.relative_preview.collapsed_instance_count,
                    "relative_preview_source_file_count": response.relative_preview.source_file_count,
                    "relative_diversity_bucket_count": response.relative_preview.diversity_bucket_count,
                    "conditional_relative_count": sum(1 for item in _iter_bucket_relative_summaries(response.relative_preview) if item.condition is not None),
                    "context_line_count": len(response.source_context.lines) if response.source_context is not None else 0,
                    "source_context_line_dropped_count": response.source_context_line_dropped_count,
                    "truncated_count": _truncated_count(response),
                },
                payload={
                    "operation": "detail",
                    "outcome": "read",
                    "budget": budget,
                    "response_truncated": response.response_truncated,
                    "budget_exhausted": response.relative_preview.budget_exhausted,
                    "budget_exhausted_kind": response.relative_preview.budget_exhausted_kind,
                    **response.view_state.to_json(),
                },
            )
        )

    def _emit_error(
        self,
        code: str,
        message: str,
        *,
        status: str,
        tool_name: Optional[str] = None,
        started: Optional[float] = None,
    ) -> None:
        self._write_event(
            LogEvent(
                event_name="mcp.error",
                channel="mcp",
                status=status,
                error_code=code,
                duration_ms=_elapsed_ms(started) if started else None,
                counts={"error_count": 1},
                payload={
                    "operation": "error",
                    "outcome": "failed",
                    "method": "tools/call",
                    "tool_name": tool_name,
                    "request_kind": "tool_call",
                    "error_code": code,
                },
                summary=message,
            )
        )

    def _write_event(self, event: LogEvent) -> None:
        if not self.log_enabled:
            return
        try:
            open_log(self.target_repo).write_event(event)
        except LogError:
            return


def open_mcp_server(
    target_repo: Path,
    *,
    log_enabled: bool = True,
    fact_view_provider: Optional[Callable[[], object]] = None,
) -> McpServer:
    return McpServer(Path(target_repo), log_enabled=log_enabled, fact_view_provider=fact_view_provider)


def serve_stdio(
    target_repo: Path,
    *,
    log_enabled: bool = True,
    input: TextIO = sys.stdin,
    output: TextIO = sys.stdout,
) -> int:
    server = open_mcp_server(target_repo, log_enabled=log_enabled)
    for raw_line in input:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = _json_rpc_error(None, "malformed_json", "invalid JSON")
        else:
            response = server.handle_message(message)
        if response is not None:
            output.write(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            output.flush()
    return 0


def _validate_search_request(query: object, limit: object) -> SearchRequest:
    if not isinstance(query, str):
        raise McpError("invalid_query", "search.query must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 50:
        raise McpError("invalid_limit", "search.limit must be an integer between 1 and 50")
    return SearchRequest(query=query, limit=limit)


def _validate_detail_request(fact_id: object, budget: object) -> DetailRequest:
    if budget not in BUDGETS:
        raise McpError("invalid_budget", "detail.budget must be small, normal, or large")
    if not isinstance(fact_id, str) or not fact_id:
        raise McpError("invalid_fact_id", "detail.fact_id must be a non-empty string")
    return DetailRequest(fact_id=fact_id, budget=str(budget))


def _reject_unknown_args(arguments: Dict[str, JSONValue], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise McpError("invalid_args", f"unsupported argument: {unknown[0]}", details={"argument": unknown[0]})


def _summary_from_fact(fact: FactRecord) -> FactSummary:
    preview, truncated = _bounded_payload(fact.payload, "small", max_fields=SEARCH_PREVIEW_FIELDS, max_chars=SEARCH_PREVIEW_CHARS)
    return FactSummary(
        object_id=fact.object_id,
        object_name=fact.object_name,
        object_description=fact.object_description,
        object_source=fact.object_source,
        object_profile=fact.object_profile,
        object_caller=fact.object_caller,
        object_callee=fact.object_callee,
        payload_preview=preview,
        truncated=truncated,
    )


def _summary_from_relation_match(match: RelationSearchMatch) -> RelationEndpointSummary:
    relation = match.matched_relations[0] if match.matched_relations else None
    return RelationEndpointSummary(
        object_id=match.fact.object_id,
        object_name=match.fact.object_name,
        object_source=match.fact.object_source,
        relation_kind=relation.relation_kind if relation is not None else "",
        instances=match.instances,
        representative_relative_id=match.representative_relative_id,
        hop=match.hop,
    )


def _summary_from_relation_path_node(node) -> RelationPathSummary:
    return RelationPathSummary(
        object_id=node.fact.object_id,
        object_name=node.fact.object_name,
        object_source=node.fact.object_source,
        hop=node.hop,
        relation_kind=node.relation_kind,
        representative_relative_id=node.representative_relative_id,
        condition=node.condition.to_json() if node.condition is not None else None,
    )


def _summary_from_anchor_candidate(candidate: RelationSearchAnchorCandidate) -> FactSummary:
    summary = _summary_from_fact(candidate.fact)
    preview = dict(summary.payload_preview)
    preview["resolution_tier"] = candidate.resolution_tier
    preview["exact_name"] = candidate.exact_name
    preview["anchor_match"] = "fuzzy" if candidate.resolution_tier >= 3 else "exact"
    preview["anchor_role"] = candidate.role
    preview["anchor_owner"] = _anchor_owner_label(candidate.fact)
    return FactSummary(
        object_id=summary.object_id,
        object_name=summary.object_name,
        object_description=summary.object_description,
        object_source=summary.object_source,
        object_profile=summary.object_profile,
        object_caller=summary.object_caller,
        object_callee=summary.object_callee,
        payload_preview=preview,
        truncated=summary.truncated,
    )


def _search_response_from_relation_result(
    view_state: ViewStateMetadata,
    query: str,
    limit: int,
    result: RelationSearchResult,
) -> SearchResponse:
    results = [_summary_from_relation_match(match) for match in result.matches]
    anchor = _summary_from_fact(result.anchor) if result.anchor is not None else None
    candidates = [_summary_from_anchor_candidate(candidate) for candidate in result.anchor_candidates]
    return SearchResponse(
        view_state=view_state,
        query=query,
        limit=limit,
        result_count=len(results),
        truncated=result.status == "too_broad",
        results=results,
        status=result.status,
        query_kind=result.query_kind,
        relation=result.query.predicate,
        anchor=anchor,
        total=result.total,
        message=_relation_search_message(result) if result.status in {"too_broad", "needs_refinement"} or result.message else None,
        available_filters=_relation_available_filters(result) if result.status == "too_broad" else [],
        examples=_relation_examples(result),
        anchor_candidates=candidates,
        matched_endpoint_count=result.matched_endpoint_count if result.matched_endpoint_count is not None else result.total,
        complete=result.complete,
        budget_exhausted=result.budget_exhausted,
        budget_exhausted_kind=result.budget_exhausted_kind,
        total_is_exact=result.total_is_exact,
        reachable=result.reachable,
        path=[_summary_from_relation_path_node(node) for node in result.path],
        depth_requested=result.depth_requested,
        depth_used=result.depth_used,
        depth_max=result.depth_max,
        visited_function_count=result.visited_function_count,
        frontier_edge_count=result.frontier_edge_count,
        skipped_missing_endpoint_count=result.skipped_missing_endpoint_count,
    )


def _relation_available_filters(result: RelationSearchResult) -> List[str]:
    if result.query.query_kind == "relation_reachable":
        return []
    if result.query.file_filters:
        return []
    return ["file:<path>"]


def _relation_filter_count(response: SearchResponse) -> int:
    try:
        query = parse_relation_search_query(response.query)
    except StorageError:
        return 0
    if query is None:
        return 0
    return len(query.file_filters) + len(query.name_filters) + len(query.terms)


def _relation_search_message(result: RelationSearchResult) -> str:
    if result.message is not None:
        return result.message
    if result.status == "needs_refinement":
        if result.anchor_candidates:
            candidates = "; ".join(_anchor_candidate_text(candidate) for candidate in result.anchor_candidates[:5])
            if all(candidate.resolution_tier >= 3 for candidate in result.anchor_candidates):
                return (
                    f"No exact {result.query.anchor_kind} anchor matched `{result.query.anchor}`; "
                    f"choose one returned object_id and rerun {result.query.predicate}:<object_id>. "
                    f"Candidates: {candidates}."
                )
            return (
                f"{len(result.anchor_candidates)} possible {result.query.anchor_kind} anchors for `{result.query.anchor}`; "
                f"choose one returned object_id and rerun {result.query.predicate}:<object_id>. "
                f"Candidates: {candidates}."
            )
        return "No exact anchor was selected; refine with an object_id from search results."
    noun = _relation_result_noun(result.query.predicate)
    if result.query.file_filters:
        return (
            f"{result.total} matching {noun} exceeds the bounded search result window; "
            "returned results are the most salient subset. Report the returned subset with the total, "
            "and only narrow further when checking a known specific function. Do not guess function names to enumerate."
        )
    return f"{result.total} matching {noun} is too broad to enumerate; add a file:<path> filter."


def _relation_result_noun(predicate: str) -> str:
    return {
        "readers": "readers",
        "writers": "writers",
        "accessors": "accessors",
        "dispatches_via": "dispatch targets",
        "callers": "callers",
        "callees": "callees",
    }.get(predicate, "results")


def _relation_examples(result: RelationSearchResult) -> List[str]:
    if result.examples:
        return list(result.examples)
    if result.status == "needs_refinement" and result.anchor_candidates:
        base = _relation_refinement_example_base(result, result.anchor_candidates[0])
        return [f"search('{base}')"]
    base = _relation_example_base(result)
    if result.query.query_kind == "relation_reachable":
        return [f"search('{base} depth:{min(result.query.depth, 3)}')"]
    if result.status == "too_broad" and result.query.file_filters:
        return []
    filters = []
    if result.query.file_filters:
        filters.append(f"file:{result.query.file_filters[0]}")
    else:
        filters.append("file:<path>")
    return [f"search('{base} {' '.join(filters)}')"]


def _relation_example_base(result: RelationSearchResult) -> str:
    if result.query.predicate == "reachable" and result.query.target_anchor is not None:
        start = result.anchor.object_id if result.anchor is not None else result.query.anchor
        return f"reachable:{start}->{result.query.target_anchor}"
    anchor = result.anchor.object_id if result.anchor is not None else result.query.anchor
    return f"{result.query.predicate}:{anchor}"


def _relation_refinement_example_base(
    result: RelationSearchResult,
    candidate: RelationSearchAnchorCandidate,
) -> str:
    candidate_id = candidate.fact.object_id
    if result.query.predicate == "reachable" and result.query.target_anchor is not None:
        if candidate.role == "target":
            start = result.anchor.object_id if result.anchor is not None else result.query.anchor
            return f"reachable:{start}->{candidate_id}"
        return f"reachable:{candidate_id}->{result.query.target_anchor}"
    return f"{result.query.predicate}:{candidate_id}"


def _anchor_candidate_text(candidate: RelationSearchAnchorCandidate) -> str:
    owner = _anchor_owner_label(candidate.fact)
    source = candidate.fact.object_source or "<unknown-source>"
    return f"(object_id={candidate.fact.object_id}, owner={owner}, source={source})"


def _anchor_owner_label(fact: FactRecord) -> str:
    for key in ("owner_name", "type", "owner_type_id"):
        value = fact.payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "-"


def _plain_search_message(query: str, records: List[FactRecord]) -> Optional[str]:
    query_text = query.strip()
    if not EXACT_OBJECT_NAME_QUERY_RE.fullmatch(query_text) or not records:
        return None
    query_key = query_text.casefold()
    if any(record.object_name.casefold() == query_key for record in records):
        return None
    names: List[str] = []
    seen = set()
    for record in records[:3]:
        if record.object_name in seen:
            continue
        seen.add(record.object_name)
        names.append(record.object_name)
    suffix = f" Top text matches: {', '.join(names)}." if names else ""
    return f"No exact object_name match for `{query_text}`; returned text matches are fallback candidates.{suffix}"


def _summary_from_relative(
    relative: FactRelative,
    *,
    conditions: Optional[List[Dict[str, JSONValue]]] = None,
    instances: int = 1,
    endpoint_name: Optional[str] = None,
    endpoint_profile: Optional[str] = None,
    endpoint_source: Optional[str] = None,
) -> RelativeSummary:
    preview, truncated = _bounded_payload(relative.payload, "small", max_fields=SEARCH_PREVIEW_FIELDS, max_chars=SEARCH_PREVIEW_CHARS)
    return RelativeSummary(
        relative_id=relative.relative_id,
        from_fact_id=relative.from_fact_id,
        to_fact_id=relative.to_fact_id,
        relation_kind=relative.relation_kind,
        condition=relative.condition.to_json() if relative.condition is not None else None,
        evidence_source=relative.evidence_source,
        confidence=float(relative.confidence),
        payload_preview=preview,
        truncated=truncated,
        conditions=list(conditions or []),
        instances=instances,
        endpoint_name=endpoint_name,
        endpoint_profile=endpoint_profile,
        endpoint_source=endpoint_source,
    )


def _select_relative_summaries(
    store,
    relatives: List[FactRelative],
    direction: str,
    relation_kind: str,
    bucket_limit: int,
) -> Tuple[List[RelativeSummary], int, int, set[str], bool]:
    rollups = _rollup_relatives(store, relatives, direction, relation_kind)
    sorted_rollups = sorted(rollups, key=_relative_rollup_sort_key)
    selected = _select_diverse_rollups(sorted_rollups, bucket_limit)
    naive = sorted_rollups[:bucket_limit]
    diversity_applied = [rollup.key for rollup in selected] != [rollup.key for rollup in naive]
    summaries = [
        _summary_from_relative(
            rollup.representative,
            conditions=_sorted_conditions(rollup),
            instances=rollup.instances,
            endpoint_name=rollup.endpoint_name,
            endpoint_profile=rollup.endpoint_profile,
            endpoint_source=rollup.endpoint_source,
        )
        for rollup in selected
    ]
    collapsed = sum(max(0, rollup.instances - 1) for rollup in rollups)
    source_files = {rollup.endpoint_source_file for rollup in selected}
    return summaries, len(rollups), collapsed, source_files, diversity_applied


def _rollup_relatives(
    store,
    relatives: List[FactRelative],
    direction: str,
    relation_kind: str,
) -> List[_RelativeRollup]:
    rollups: Dict[Tuple[str, str, str], _RelativeRollup] = {}
    for relative in relatives:
        endpoint_id = relative.from_fact_id if direction == "incoming" else relative.to_fact_id
        key = (direction, endpoint_id, relation_kind)
        rollup = rollups.get(key)
        if rollup is None:
            endpoint_name, endpoint_profile, endpoint_source, endpoint_source_file, endpoint_missing = _endpoint_metadata(store, endpoint_id)
            rollup = _RelativeRollup(
                key=key,
                endpoint_id=endpoint_id,
                endpoint_name=endpoint_name,
                endpoint_profile=endpoint_profile,
                endpoint_source=endpoint_source,
                endpoint_source_file=endpoint_source_file,
                endpoint_missing=endpoint_missing,
                direction=direction,
                relation_kind=relation_kind,
                representative=relative,
            )
            rollups[key] = rollup
        rollup.instances += 1
        if relative.condition is None:
            rollup.has_unconditional = True
        else:
            condition = relative.condition.to_json()
            rollup.conditions[_canonical_json(condition)] = condition
        if _relative_representative_key(relative) < _relative_representative_key(rollup.representative):
            rollup.representative = relative
    return list(rollups.values())


def _endpoint_metadata(store, endpoint_id: str) -> Tuple[str, Optional[str], Optional[str], str, bool]:
    fact = store.get_fact(endpoint_id)
    if fact is None:
        return endpoint_id, None, None, "<missing-endpoint>", True
    source = fact.object_source
    return fact.object_name, fact.object_profile, source, _endpoint_source_file(source), False


def _endpoint_source_file(object_source: Optional[str]) -> str:
    if not object_source:
        return "<unknown-source>"
    path, separator, line = object_source.rpartition(":")
    if separator and path and line.isdigit() and int(line) > 0:
        return path
    return object_source


def _relative_representative_key(relative: FactRelative) -> Tuple[int, str, str]:
    return (1 if relative.condition is not None else 0, relative.evidence_source, relative.relative_id)


def _relative_rollup_sort_key(rollup: _RelativeRollup) -> Tuple[int, int, int, int, str, str, str]:
    return (
        RELATIVE_SALIENCE_RANKS.get(rollup.relation_kind, 5),
        -rollup.instances,
        0 if rollup.has_unconditional else 1,
        1 if rollup.endpoint_missing else 0,
        rollup.endpoint_name,
        rollup.endpoint_source_file,
        rollup.representative.relative_id,
    )


def _select_diverse_rollups(rollups: List[_RelativeRollup], bucket_limit: int) -> List[_RelativeRollup]:
    if len(rollups) <= bucket_limit:
        return list(rollups)
    selected: List[_RelativeRollup] = []
    selected_keys = set()
    source_counts: Dict[str, int] = {}
    for rollup in rollups:
        source_count = source_counts.get(rollup.endpoint_source_file, 0)
        if source_count >= RELATIVE_PREVIEW_SOURCE_SOFT_CAP:
            continue
        selected.append(rollup)
        selected_keys.add(rollup.key)
        source_counts[rollup.endpoint_source_file] = source_count + 1
        if len(selected) >= bucket_limit:
            return selected
    for rollup in rollups:
        if rollup.key in selected_keys:
            continue
        selected.append(rollup)
        if len(selected) >= bucket_limit:
            break
    return selected


def _sorted_conditions(rollup: _RelativeRollup) -> List[Dict[str, JSONValue]]:
    return [rollup.conditions[key] for key in sorted(rollup.conditions)]


def _canonical_json(value: JSONValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _relative_preview_keys() -> List[Tuple[str, str]]:
    keys = list(RELATIVE_PREVIEW_ORDER)
    seen = set(keys)
    for relation_kind in sorted(RELATION_KINDS):
        for direction in ("incoming", "outgoing"):
            key = (direction, relation_kind)
            if key not in seen:
                keys.append(key)
                seen.add(key)
    return keys


def _relative_bucket_name(direction: str, relation_kind: str) -> str:
    names = {
        ("incoming", "direct_call"): "callers",
        ("outgoing", "direct_call"): "callees",
        ("incoming", "field_read"): "field_readers",
        ("incoming", "field_write"): "field_writers",
        ("outgoing", "field_read"): "fields_read",
        ("outgoing", "field_write"): "fields_written",
        ("incoming", "has_field"): "field_owner",
        ("outgoing", "has_field"): "fields",
        ("incoming", "assigned_to"): "assigners",
        ("outgoing", "assigned_to"): "assigned_targets",
        ("incoming", "dispatches_via"): "dispatch_sources",
        ("outgoing", "dispatches_via"): "dispatch_targets",
    }
    return names.get((direction, relation_kind), f"{direction}_{relation_kind}")


def _count_relatives_for_fact(
    store,
    fact_id: str,
    direction: str,
    relation_kind: str,
    fetched: List[FactRelative],
) -> int:
    counter = getattr(store, "count_relatives_for_fact", None)
    if callable(counter):
        return int(counter(fact_id, direction=direction, relation_kind=relation_kind))
    return len(fetched)


def _enforce_detail_response_budget(response: DetailResponse, budget: str) -> DetailResponse:
    limit = int(BUDGETS[budget]["response_bytes"])
    original_bucket_count = _bucket_relative_count(response.relative_preview)
    original_source_line_count = _source_context_line_count(response.source_context)
    original_payload_field_count = len(response.payload)
    response = _refresh_detail_budget_metrics(
        response,
        limit=limit,
        response_truncated=False,
        original_bucket_count=original_bucket_count,
        original_source_line_count=original_source_line_count,
        original_payload_field_count=original_payload_field_count,
    )
    if response.response_bytes <= limit:
        return response

    for flat_limit in _decreasing_limits(len(response.relative_preview.relatives)):
        response = replace(response, relative_preview=_with_flat_relative_limit(response.relative_preview, flat_limit))
        response = _refresh_detail_budget_metrics(
            response,
            limit=limit,
            response_truncated=True,
            original_bucket_count=original_bucket_count,
            original_source_line_count=original_source_line_count,
            original_payload_field_count=original_payload_field_count,
        )
        if response.response_bytes <= limit:
            return response

    for bucket_index in _relative_bucket_drop_order(response.relative_preview):
        response = replace(response, relative_preview=_with_dropped_relative_bucket(response.relative_preview, bucket_index))
        response = _refresh_detail_budget_metrics(
            response,
            limit=limit,
            response_truncated=True,
            original_bucket_count=original_bucket_count,
            original_source_line_count=original_source_line_count,
            original_payload_field_count=original_payload_field_count,
        )
        if response.response_bytes <= limit:
            return response

    while _source_context_line_count(response.source_context) > 0:
        next_line_limit = _source_context_line_count(response.source_context) // 2
        response = replace(
            response,
            source_context=_with_source_context_line_limit(
                response.source_context,
                response.fact.object_source,
                next_line_limit,
            ),
        )
        response = _refresh_detail_budget_metrics(
            response,
            limit=limit,
            response_truncated=True,
            original_bucket_count=original_bucket_count,
            original_source_line_count=original_source_line_count,
            original_payload_field_count=original_payload_field_count,
        )
        if response.response_bytes <= limit:
            return response

    while response.payload:
        next_field_limit = len(response.payload) // 2
        response = replace(
            response,
            payload=_with_payload_field_limit(response.payload, next_field_limit),
            payload_truncated=True,
        )
        response = _refresh_detail_budget_metrics(
            response,
            limit=limit,
            response_truncated=True,
            original_bucket_count=original_bucket_count,
            original_source_line_count=original_source_line_count,
            original_payload_field_count=original_payload_field_count,
        )
        if response.response_bytes <= limit:
            return response

    for fact_char_limit in (128, 64, 32, 16, 0):
        response = replace(response, fact=_with_fact_summary_char_limit(response.fact, fact_char_limit))
        response = _refresh_detail_budget_metrics(
            response,
            limit=limit,
            response_truncated=True,
            original_bucket_count=original_bucket_count,
            original_source_line_count=original_source_line_count,
            original_payload_field_count=original_payload_field_count,
        )
        if response.response_bytes <= limit:
            return response
    return response


def _refresh_detail_budget_metrics(
    response: DetailResponse,
    *,
    limit: int,
    response_truncated: bool,
    original_bucket_count: int,
    original_source_line_count: int,
    original_payload_field_count: int,
) -> DetailResponse:
    preview = response.relative_preview
    bucket_count = _bucket_relative_count(preview)
    preview = replace(
        preview,
        shown_count=bucket_count,
        truncated=preview.truncated or bucket_count < preview.total_count or len(preview.relatives) < bucket_count,
        flat_relative_count=len(preview.relatives),
        flat_relative_dropped_count=max(0, bucket_count - len(preview.relatives)),
        bucket_relative_dropped_count=max(0, original_bucket_count - bucket_count),
        bucket_dropped_count=_dropped_relative_bucket_count(preview),
        budget_exhausted=response_truncated,
        budget_exhausted_kind="response_bytes" if response_truncated else None,
    )
    response = replace(
        response,
        relative_preview=preview,
        response_bytes_limit=limit,
        response_truncated=response_truncated,
        source_context_line_dropped_count=max(0, original_source_line_count - _source_context_line_count(response.source_context)),
        payload_field_dropped_count=max(0, original_payload_field_count - len(response.payload)),
    )
    return replace(response, response_bytes=_detail_response_json_bytes(response))


def _detail_response_json_bytes(response: DetailResponse) -> int:
    return len(_canonical_json(response.to_json()).encode("utf-8"))


def _decreasing_limits(current: int):
    while current > 0:
        current = max(0, current // 2)
        yield current


def _relative_compat_sample(buckets: List[RelationPreviewBucket], limit: int) -> List[RelativeSummary]:
    if limit <= 0:
        return []
    selected: List[RelativeSummary] = []
    positions = [0 for _bucket in buckets]
    while len(selected) < limit:
        made_progress = False
        for index, bucket in enumerate(buckets):
            if positions[index] >= len(bucket.relatives):
                continue
            selected.append(bucket.relatives[positions[index]])
            positions[index] += 1
            made_progress = True
            if len(selected) >= limit:
                break
        if not made_progress:
            break
    return selected


def _with_flat_relative_limit(preview: RelationPreview, limit: int) -> RelationPreview:
    return replace(preview, relatives=preview.relatives[: max(0, limit)])


def _relative_bucket_drop_order(preview: RelationPreview) -> List[int]:
    order_index = {
        (direction, relation_kind): index
        for index, (direction, relation_kind) in enumerate(_relative_preview_keys())
    }
    indexed = [(index, bucket) for index, bucket in enumerate(preview.buckets) if bucket.relatives]
    indexed.sort(
        key=lambda item: (
            RELATIVE_SALIENCE_RANKS.get(item[1].relation_kind, 5),
            order_index.get((item[1].direction, item[1].relation_kind), -1),
        ),
        reverse=True,
    )
    return [index for index, _bucket in indexed]


def _with_dropped_relative_bucket(preview: RelationPreview, bucket_index: int) -> RelationPreview:
    buckets: List[RelationPreviewBucket] = []
    for index, bucket in enumerate(preview.buckets):
        if index == bucket_index:
            buckets.append(replace(bucket, shown_count=0, truncated=True, relatives=[]))
        else:
            buckets.append(bucket)
    flat_limit = min(len(preview.relatives), RELATIVE_PREVIEW_FLAT_LIMIT)
    shown_count = _bucket_relative_count_from_buckets(buckets)
    return replace(
        preview,
        buckets=buckets,
        relatives=_relative_compat_sample(buckets, flat_limit),
        shown_count=shown_count,
        truncated=True,
    )


def _dropped_relative_bucket_count(preview: RelationPreview) -> int:
    return sum(1 for bucket in preview.buckets if bucket.total_count > 0 and not bucket.relatives)


def _bucket_relative_count(preview: RelationPreview) -> int:
    return _bucket_relative_count_from_buckets(preview.buckets)


def _bucket_relative_count_from_buckets(buckets: List[RelationPreviewBucket]) -> int:
    return sum(len(bucket.relatives) for bucket in buckets)


def _iter_bucket_relative_summaries(preview: RelationPreview):
    for bucket in preview.buckets:
        for relative in bucket.relatives:
            yield relative


def _source_context_line_count(context: Optional[SourceContext]) -> int:
    return len(context.lines) if context is not None else 0


def _with_source_context_line_limit(
    context: Optional[SourceContext],
    object_source: str,
    max_lines: int,
) -> Optional[SourceContext]:
    if context is None or not context.lines:
        return context
    max_lines = max(0, max_lines)
    if max_lines == 0:
        return replace(context, start_line=None, end_line=None, lines=[], truncated=True)
    available_start = context.start_line or 1
    available_end = context.end_line or (available_start + len(context.lines) - 1)
    parsed = _parse_object_source(object_source)
    anchor_line = parsed[1] if parsed is not None else (available_start + available_end) // 2
    if anchor_line < available_start or anchor_line > available_end:
        anchor_line = (available_start + available_end) // 2
    start = max(available_start, anchor_line - (max_lines // 2))
    end = min(available_end, start + max_lines - 1)
    start = max(available_start, end - max_lines + 1)
    offset_start = max(0, start - available_start)
    offset_end = max(offset_start, end - available_start + 1)
    return replace(
        context,
        start_line=start,
        end_line=end,
        lines=context.lines[offset_start:offset_end],
        truncated=True,
    )


def _with_payload_field_limit(payload: Dict[str, JSONValue], max_fields: int) -> Dict[str, JSONValue]:
    if max_fields <= 0:
        return {}
    return {key: payload[key] for key in sorted(payload)[:max_fields]}


def _with_fact_summary_char_limit(summary: FactSummary, max_chars: int) -> FactSummary:
    object_name, name_truncated = _truncate_text(summary.object_name, max_chars)
    description, description_truncated = _truncate_text(summary.object_description, max_chars)
    source, source_truncated = _truncate_text(summary.object_source, max_chars)
    profile, profile_truncated = _truncate_text(summary.object_profile, max_chars)
    payload_preview, payload_truncated = _bound_value(summary.payload_preview, max_chars)
    return replace(
        summary,
        object_name=object_name,
        object_description=description,
        object_source=source,
        object_profile=profile,
        payload_preview=payload_preview if isinstance(payload_preview, dict) else {},
        truncated=summary.truncated
        or name_truncated
        or description_truncated
        or source_truncated
        or profile_truncated
        or payload_truncated,
    )


def _truncate_text(value: str, max_chars: int) -> Tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    if max_chars <= 0:
        return "", True
    return value[:max_chars] + "...[TRUNCATED]", True


def _bounded_payload(
    payload: Dict[str, JSONValue],
    budget: str,
    *,
    max_fields: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> Tuple[Dict[str, JSONValue], bool]:
    limits = BUDGETS[budget]
    field_limit = max_fields if max_fields is not None else int(limits["payload_fields"])
    char_limit = max_chars if max_chars is not None else int(limits["string_chars"])
    items = sorted(payload.items(), key=lambda item: item[0])
    result: Dict[str, JSONValue] = {}
    truncated = len(items) > field_limit
    for key, value in items[:field_limit]:
        bounded, value_truncated = _bound_value(value, char_limit)
        result[str(key)] = bounded
        truncated = truncated or value_truncated
    return result, truncated


def _bound_value(value: JSONValue, max_chars: int) -> Tuple[JSONValue, bool]:
    if isinstance(value, str):
        if len(value) > max_chars:
            return value[:max_chars] + "...[TRUNCATED]", True
        return value, False
    if isinstance(value, list):
        output: List[JSONValue] = []
        truncated = len(value) > 8
        for item in value[:8]:
            bounded, child_truncated = _bound_value(item, max_chars)
            output.append(bounded)
            truncated = truncated or child_truncated
        return output, truncated
    if isinstance(value, dict):
        output: Dict[str, JSONValue] = {}
        items = sorted(value.items(), key=lambda item: item[0])
        truncated = len(items) > 8
        for key, item in items[:8]:
            bounded, child_truncated = _bound_value(item, max_chars)
            output[str(key)] = bounded
            truncated = truncated or child_truncated
        return output, truncated
    return value, False


def _source_context(target_repo: Path, object_source: str, budget: str) -> SourceContext:
    parsed = _parse_object_source(object_source)
    if parsed is None:
        return SourceContext(object_source, None, None, [], False, "unrecognized_source_format")
    rel_path, line_number = parsed
    target_root = target_repo.resolve(strict=False)
    source_path = (target_repo / rel_path).resolve(strict=False)
    if not _is_relative_to(source_path, target_root):
        return SourceContext(rel_path, None, None, [], False, "source_path_escape")
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return SourceContext(rel_path, None, None, [], False, "source_unreadable")
    if line_number < 1 or line_number > len(lines):
        return SourceContext(rel_path, None, None, [], False, "source_unreadable")
    radius = int(BUDGETS[budget]["source_radius"])
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    return SourceContext(
        source=rel_path,
        start_line=start,
        end_line=end,
        lines=lines[start - 1 : end],
        truncated=start > 1 or end < len(lines),
        unavailable_reason=None,
    )


def _parse_object_source(object_source: str) -> Optional[Tuple[str, int]]:
    if not isinstance(object_source, str) or ":" not in object_source:
        return None
    rel_path, line_text = object_source.rsplit(":", 1)
    if not rel_path or rel_path.startswith("/") or not line_text.isdecimal():
        return None
    line_number = int(line_text)
    if line_number < 1:
        return None
    return rel_path, line_number


def _search_text(response: SearchResponse) -> str:
    prefix = f"snapshot {response.view_state.base_snapshot_id or 'none'} view_state={response.view_state.view_state}: "
    if response.status == "too_broad":
        return prefix + (response.message or f"search relation {response.relation} is too broad")
    if response.status == "needs_refinement":
        return prefix + (response.message or f"search relation {response.relation} needs refinement")
    if not response.results:
        message = f"{response.message} " if response.message else ""
        return prefix + f"{message}search returned 0 fact results for query kind {response.query_kind}"
    ids = ", ".join(result.object_id for result in response.results)
    suffix = " truncated" if response.truncated else ""
    message = f"{response.message} " if response.message else ""
    return prefix + f"{message}search returned {response.result_count} fact results{suffix}: {ids}"


def _detail_text(response: DetailResponse) -> str:
    prefix = f"snapshot {response.view_state.base_snapshot_id or 'none'} view_state={response.view_state.view_state}: "
    context = response.source_context
    if context is not None and context.unavailable_reason:
        return prefix + f"detail {response.fact.object_id}: source unavailable ({context.unavailable_reason})"
    line_count = len(context.lines) if context is not None else 0
    suffixes = []
    if response.payload_truncated:
        suffixes.append("payload truncated")
    if response.response_truncated:
        suffixes.append("response truncated")
    suffix = f" ({', '.join(suffixes)})" if suffixes else ""
    relation_count = response.relative_preview.shown_count
    relation_total = response.relative_preview.total_count
    return (
        prefix
        + f"detail {response.fact.object_id}: payload_fields={len(response.payload)} "
        + f"source_lines={line_count} relatives={relation_count}/{relation_total}{suffix}"
    )


def _view_state_from_view(view) -> ViewStateMetadata:
    overlay = getattr(view, "_overlay", None)
    return ViewStateMetadata(
        view_state=getattr(view, "view_state", "base"),
        base_snapshot_id=getattr(view, "base_snapshot_id", None),
        overlay_id=getattr(view, "overlay_id", None),
        stale_source_count=getattr(overlay, "stale_source_count", 0) if overlay is not None else 0,
        pending_task_count=getattr(overlay, "pending_task_count", 0) if overlay is not None else 0,
    )


def _error_result(error: McpError) -> ToolCallResult:
    return ToolCallResult(
        content=[{"type": "text", "text": f"{error.code}: {error.message}"}],
        structured_content={"error": error.to_json()},
        is_error=True,
    )


def _json_rpc_result(request_id: object, result: Dict[str, JSONValue]) -> Dict[str, JSONValue]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _json_rpc_error(request_id: object, code: str, message: str) -> Dict[str, JSONValue]:
    numeric = -32700 if code == "malformed_json" else -32601 if code in {"unknown_method", "unknown_tool"} else -32600
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": numeric, "message": message, "data": {"code": code}}}


def _truncated_count(response: DetailResponse) -> int:
    count = 1 if response.payload_truncated else 0
    if response.source_context is not None and response.source_context.truncated:
        count += 1
    if (
        response.relative_preview.truncated
        or any(bucket.truncated for bucket in response.relative_preview.buckets)
        or any(relative.truncated for relative in response.relative_preview.relatives)
        or any(relative.truncated for relative in _iter_bucket_relative_summaries(response.relative_preview))
    ):
        count += 1
    if response.response_truncated:
        count += 1
    return count


def _query_kind(query: str) -> str:
    try:
        if parse_relation_search_query(query) is not None:
            return "relation"
    except StorageError:
        return "relation"
    return "empty" if not _search_terms(query) else "terms"


def _search_terms(query: str) -> List[str]:
    return [term.casefold() for term in query.split() if term]


def _returned_ids(response: SearchResponse) -> List[str]:
    return [item.object_id for item in response.results if isinstance(getattr(item, "object_id", None), str)]


def _elapsed_ms(started: Optional[float]) -> float:
    if started is None:
        return 0.0
    return max(0.0, (time.perf_counter() - started) * 1000)


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


__all__ = [
    "DetailRequest",
    "DetailResponse",
    "FactSummary",
    "McpError",
    "McpServer",
    "RelationPreview",
    "RelationPreviewBucket",
    "RelativeSummary",
    "SearchRequest",
    "SearchResponse",
    "SourceContext",
    "ToolCallResult",
    "ToolDescriptor",
    "ToolListResponse",
    "open_mcp_server",
    "serve_stdio",
]

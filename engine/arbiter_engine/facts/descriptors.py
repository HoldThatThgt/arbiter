"""Byte-frozen facts tool descriptors absorbed from cipher-2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

JSONValue = Any


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    title: str
    description: str
    input_schema: Dict[str, JSONValue]
    output_schema: Optional[Dict[str, JSONValue]] = None

    def to_json(self) -> Dict[str, JSONValue]:
        row: Dict[str, JSONValue] = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.output_schema is not None:
            row["outputSchema"] = self.output_schema
        return row


def search_descriptor() -> Dict[str, JSONValue]:
    return _search_descriptor().to_json()


def detail_descriptor() -> Dict[str, JSONValue]:
    return _detail_descriptor().to_json()


def tool_descriptors() -> List[Dict[str, JSONValue]]:
    return [search_descriptor(), detail_descriptor()]


def _search_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        name="search",
        title="Search",
        description=(
            "Search FACT records with multi-term AND matching, or use relation predicates in query: "
            "readers:<field>, writers:<field>, accessors:<field>, dispatches_via:<field>, callers:<func>, callees:<func>. "
            "For field relations, first search the field name or owner terms, then use the returned field object_id as the "
            "relation anchor; do not invent owner-qualified field-name anchors. "
            "Use callers/callees with depth:<N> for bounded transitive call closure including dispatch edges, "
            "or reachable:<from>-><to> for shortest-path reachability across direct and dispatch calls. "
            "reachable path nodes may include condition, a nullable local branch/guard for that hop; "
            "multi-hop paths require the logical AND of hop conditions. "
            "Example: reachable:funcC->funcClearA path[2].condition={kind:branch,expression:reset_flag,branch:then} "
            "means that hop occurs only when reset_flag is true. "
            "Filter relation results with file:<path> and use name:<func> only when checking a known endpoint. "
            "Workflow: search('value NullableDatum') -> copy the field result.object_id -> search('writers:<field_object_id>') "
            "or detail('<field_object_id>') to inspect readers/writers/callers together. "
            "If writers:<field_object_id> returns no writes, try accessors:<field_object_id>. "
            "For too_broad or anchor ambiguity, use returned object_id candidates/examples or add file:<path>. "
            "limit is 1..50; relation results include total, complete, and budget_exhausted metadata."
        ),
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "required": [
                "view_state",
                "base_snapshot_id",
                "overlay_id",
                "stale_source_count",
                "pending_task_count",
                "status",
                "query_kind",
                "query",
                "limit",
                "result_count",
                "truncated",
                "results",
            ],
            "properties": {
                **_view_state_schema_properties(),
                "status": {"type": "string", "enum": ["ok", "too_broad", "needs_refinement"]},
                "query_kind": {
                    "type": "string",
                    "enum": ["empty", "terms", "relation", "relation_transitive", "relation_reachable"],
                },
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "result_count": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "results": {"type": "array"},
                "relation": {"type": ["string", "null"]},
                "anchor": {"type": ["object", "null"]},
                "total": {"type": "integer"},
                "message": {"type": "string"},
                "available_filters": {"type": "array"},
                "examples": {"type": "array"},
                "top_by_salience": {"type": "array"},
                "anchor_candidates": {"type": "array"},
                "matched_endpoint_count": {"type": "integer"},
                "complete": {"type": "boolean"},
                "budget_exhausted": {"type": "boolean"},
                "budget_exhausted_kind": {"type": ["string", "null"]},
                "total_is_exact": {"type": "boolean"},
                "reachable": {"type": "boolean"},
                "path": {"type": "array"},
                "depth_requested": {"type": "integer"},
                "depth_used": {"type": "integer"},
                "depth_max": {"type": "integer"},
            },
        },
    )


def _detail_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        name="detail",
        title="Detail",
        description=(
            "Expand one FACT record after search: pass fact_id from a search result object_id. "
            "budget small/normal/large trades terse versus broader payload, source_context, and "
            "relative_preview, with a hard serialized response byte ceiling. Use detail after a "
            "search hit to read bounded source and inspect relative_preview navigation: "
            "callers/callees/field buckets expose total_count, shown_count, and truncated; the "
            "top-level relatives list is only a small compatibility sample."
        ),
        input_schema={
            "type": "object",
            "required": ["fact_id"],
            "properties": {
                "fact_id": {"type": "string", "minLength": 1},
                "budget": {"type": "string", "enum": ["small", "normal", "large"], "default": "normal"},
            },
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "required": [
                "view_state",
                "base_snapshot_id",
                "overlay_id",
                "stale_source_count",
                "pending_task_count",
                "fact",
                "payload",
                "payload_truncated",
                "source_context",
                "relative_preview",
            ],
            "properties": {
                **_view_state_schema_properties(),
                "fact": {"type": "object"},
                "payload": {"type": "object"},
                "payload_truncated": {"type": "boolean"},
                "source_context": {"type": ["object", "null"]},
                "relative_preview": {"type": "object"},
            },
        },
    )


def _view_state_schema_properties() -> Dict[str, JSONValue]:
    return {
        "view_state": {"type": "string", "enum": ["base", "stale", "pending", "overlay", "error"]},
        "base_snapshot_id": {"type": ["string", "null"]},
        "overlay_id": {"type": ["string", "null"]},
        "stale_source_count": {"type": "integer"},
        "pending_task_count": {"type": "integer"},
    }


__all__ = [
    "ToolDescriptor",
    "detail_descriptor",
    "search_descriptor",
    "tool_descriptors",
]

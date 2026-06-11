from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import threading
import uuid
from collections import Counter, OrderedDict
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from cipher2.common import JSONValue
from cipher2.tools.log import LogError, LogEvent, open_log

from .constants import *
from .models import *
from .search import *
from .serialization import *
from .utils import *

@dataclass
class _ReadIndex:
    connection: sqlite3.Connection
    lock: threading.RLock = field(default_factory=threading.RLock)

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def get_fact(self, object_id: str) -> Optional[FactRecord]:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT object_id, object_name, object_description, object_source,
                       object_profile, object_caller, object_callee, payload_json
                FROM facts
                WHERE object_id = ?
                """,
                (object_id,),
            ).fetchone()
        if row is None:
            return None
        return _fact_from_index_row(row)

    def search(self, query: str, limit: int) -> List[FactRecord]:
        with self.lock:
            terms = _search_terms(query)
            if not terms:
                rows = self.connection.execute(
                    """
                    SELECT object_id, object_name, object_description, object_source,
                           object_profile, object_caller, object_callee, payload_json
                    FROM facts
                    ORDER BY object_id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                fields = (
                    ("object_name", "object_name_cf", 3),
                    ("object_description", "object_description_cf", 2),
                    ("object_caller", "object_caller_cf", 2),
                    ("object_callee", "object_callee_cf", 2),
                    ("object_source", "object_source_cf", 1),
                )
                score_parts = []
                score_params: List[str] = []
                where_parts = []
                where_params: List[str] = []
                query_key = " ".join(terms)
                for term in terms:
                    score_parts.append(
                        "("
                        + " + ".join(
                            f"(instr(lower(coalesce({field}, '')), ?) > 0 OR ({fallback} IS NOT NULL AND instr({fallback}, ?) > 0)) * {weight}"
                            for field, fallback, weight in fields
                        )
                        + ")"
                    )
                    score_params.extend([term, term] * len(fields))
                    where_parts.append(
                        "("
                        + " OR ".join(
                            f"(instr(lower(coalesce({field}, '')), ?) > 0 OR ({fallback} IS NOT NULL AND instr({fallback}, ?) > 0))"
                            for field, fallback, _weight in fields
                        )
                        + ")"
                    )
                    where_params.extend([term, term] * len(fields))
                score_sql = " + ".join(score_parts)
                exact_name_sql = "(lower(object_name) = ? OR (object_name_cf IS NOT NULL AND object_name_cf = ?))"
                candidate_limit = _search_candidate_limit(limit)
                rows = self.connection.execute(
                    f"""
                    WITH scored AS (
                        SELECT object_id, object_name, object_description, object_source,
                               object_profile, object_caller, object_callee, payload_json,
                               ({score_sql}) AS score,
                               ({exact_name_sql}) AS exact_name,
                               fact_kind_rank AS kind_rank
                        FROM facts
                        WHERE {' AND '.join(where_parts)}
                    )
                    SELECT object_id, object_name, object_description, object_source,
                           object_profile, object_caller, object_callee, payload_json
                    FROM scored
                    ORDER BY (score + exact_name * {EXACT_NAME_SEARCH_BONUS} + kind_rank) DESC,
                             score DESC,
                             exact_name DESC,
                             kind_rank DESC,
                             object_id ASC
                    LIMIT ?
                    """,
                    (*score_params, query_key, query_key, *where_params, candidate_limit),
                ).fetchall()
                exact_rows = self.connection.execute(
                    """
                    SELECT object_id, object_name, object_description, object_source,
                           object_profile, object_caller, object_callee, payload_json
                    FROM facts
                    WHERE lower(object_name) = ? OR (object_name_cf IS NOT NULL AND object_name_cf = ?)
                    ORDER BY fact_kind_rank DESC, object_id ASC
                    LIMIT ?
                    """,
                    (query_key, query_key, candidate_limit),
                ).fetchall()
                owner_field_rows = []
                for owner_key, field_key in _owner_field_query_pairs(query):
                    owner_field_rows.extend(
                        self.connection.execute(
                            """
                            SELECT object_id, object_name, object_description, object_source,
                                   object_profile, object_caller, object_callee, payload_json
                            FROM facts
                            WHERE (lower(object_name) = ? OR (object_name_cf IS NOT NULL AND object_name_cf = ?))
                              AND instr(lower(payload_json), ?) > 0
                            ORDER BY fact_kind_rank DESC, object_id ASC
                            LIMIT ?
                            """,
                            (field_key, field_key, owner_key, candidate_limit),
                        ).fetchall()
                    )
                rows = [*rows, *exact_rows, *owner_field_rows]
        return _select_search_results([_fact_from_index_row(row) for row in rows], query, limit)

    def relatives_for_fact(
        self,
        fact_id: str,
        direction: str,
        relation_kind: Optional[str],
        limit: int,
    ) -> Tuple[List[FactRelative], int]:
        with self.lock:
            fact_k = self._fact_key_for_id_unlocked(fact_id)
            if fact_k is None:
                return [], 0
            where, params = _relative_sql_where(fact_k, direction, relation_kind)
            params.append(limit + 1)
            rows = self.connection.execute(
                f"""
                SELECT ri.relative_id, from_fact.object_id, to_fact.object_id, r.relation_kind_code,
                       r.confidence, r.object_profile, r.evidence_source, r.condition_json, r.payload_json
                FROM relatives AS r
                JOIN relative_ids AS ri ON ri.relative_k = r.relative_k
                JOIN fact_keys AS from_key ON from_key.fact_k = r.from_k
                JOIN facts AS from_fact ON from_fact.object_id = from_key.object_id
                JOIN fact_keys AS to_key ON to_key.fact_k = r.to_k
                JOIN facts AS to_fact ON to_fact.object_id = to_key.object_id
                WHERE {' AND '.join(where)}
                ORDER BY r.relation_kind_code ASC, r.from_k ASC, r.to_k ASC, r.relative_k ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        truncated_count = max(0, len(rows) - limit)
        rows = rows[:limit]
        return [_relative_from_index_row(row) for row in rows], truncated_count

    def count_relatives_for_fact(
        self,
        fact_id: str,
        direction: str,
        relation_kind: Optional[str],
    ) -> int:
        with self.lock:
            fact_k = self._fact_key_for_id_unlocked(fact_id)
            if fact_k is None:
                return 0
            where, params = _relative_sql_where(fact_k, direction, relation_kind)
            row = self.connection.execute(
                f"""
                    SELECT COUNT(*) AS relative_count
                    FROM relatives AS r
                    WHERE {' AND '.join(where)}
                """,
                params,
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def _fact_key_for_id_unlocked(self, object_id: str) -> Optional[int]:
        row = self.connection.execute(
            "SELECT fact_k FROM fact_keys WHERE object_id = ?",
            (object_id,),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def _fact_keys_for_ids_unlocked(self, object_ids: Iterable[str]) -> Dict[str, int]:
        ids = sorted(set(object_ids))
        if not ids:
            return {}
        output: Dict[str, int] = {}
        for offset in range(0, len(ids), 500):
            batch = ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self.connection.execute(
                f"SELECT object_id, fact_k FROM fact_keys WHERE object_id IN ({placeholders})",
                batch,
            ).fetchall()
            output.update((str(row[0]), int(row[1])) for row in rows)
        return output

    def relation_search(self, query: str, limit: int) -> RelationSearchResult:
        spec = parse_relation_search_query(query)
        if spec is None:
            raise StorageError("invalid_relation_query", "query is not a relation search")
        if spec.message is not None:
            return _relation_search_refinement_result(spec)
        candidates = self._relation_anchor_candidates(spec, limit)
        if not candidates:
            return RelationSearchResult(
                query=spec,
                status="ok",
                total=0,
                query_kind=spec.query_kind,
                depth_requested=spec.depth,
                depth_max=_relation_query_max_depth(spec),
            )
        bounded_candidates = tuple(candidates[:limit])
        if _relation_anchor_requires_refinement(candidates):
            return RelationSearchResult(
                query=spec,
                status="needs_refinement",
                total=0,
                anchor_candidates=bounded_candidates,
                query_kind=spec.query_kind,
                complete=False,
                depth_requested=spec.depth,
                depth_max=_relation_query_max_depth(spec),
            )
        anchor = candidates[0].fact
        if spec.query_kind == "relation_reachable":
            target_spec = _target_relation_query(spec)
            target_candidates = self._relation_anchor_candidates(target_spec, limit)
            if not target_candidates:
                return RelationSearchResult(
                    query=spec,
                    status="ok",
                    total=0,
                    anchor=anchor,
                    query_kind=spec.query_kind,
                    complete=True,
                    reachable=False,
                    depth_requested=spec.depth,
                    depth_max=_relation_query_max_depth(spec),
                )
            bounded_target_candidates = tuple(target_candidates[:limit])
            if _relation_anchor_requires_refinement(target_candidates):
                return RelationSearchResult(
                    query=spec,
                    status="needs_refinement",
                    total=0,
                    anchor=anchor,
                    anchor_candidates=bounded_target_candidates,
                    query_kind=spec.query_kind,
                    complete=False,
                    depth_requested=spec.depth,
                    depth_max=_relation_query_max_depth(spec),
                )
            return _relation_reachable_from_edge_provider(
                spec,
                anchor,
                target_candidates[0].fact,
                lambda frontier_ids: self._call_edges_for_frontier(frontier_ids, spec.direction),
            )
        if spec.query_kind == "relation_transitive" or spec.predicate in {"callers", "callees"}:
            return _relation_transitive_from_edge_provider(
                spec,
                anchor,
                lambda frontier_ids: self._call_edges_for_frontier(frontier_ids, spec.direction),
                limit,
            )
        relation_codes = [RELATION_KIND_CODES[kind] for kind in spec.relation_kinds]
        placeholders = ",".join("?" for _ in relation_codes)
        with self.lock:
            anchor_k = self._fact_key_for_id_unlocked(anchor.object_id)
            if anchor_k is None:
                return RelationSearchResult(
                    query=spec,
                    status="ok",
                    total=0,
                    anchor=anchor,
                    query_kind=spec.query_kind,
                )
            if spec.direction == "incoming":
                anchor_clause = "r.to_k = ?"
                endpoint_join = "endpoint_key.fact_k = r.from_k"
            else:
                anchor_clause = "r.from_k = ?"
                endpoint_join = "endpoint_key.fact_k = r.to_k"
            params: List[Any] = [anchor_k, *relation_codes]
            rows = self.connection.execute(
                f"""
                SELECT f.object_id, f.object_name, f.object_description, f.object_source,
                       f.object_profile, f.object_caller, f.object_callee, f.payload_json,
                       ri.relative_id, from_fact.object_id, to_fact.object_id, r.relation_kind_code,
                       r.confidence, r.object_profile, r.evidence_source, r.condition_json, r.payload_json
                FROM relatives r
                JOIN relative_ids ri ON ri.relative_k = r.relative_k
                JOIN fact_keys endpoint_key ON {endpoint_join}
                JOIN facts f ON f.object_id = endpoint_key.object_id
                JOIN fact_keys from_key ON from_key.fact_k = r.from_k
                JOIN facts from_fact ON from_fact.object_id = from_key.object_id
                JOIN fact_keys to_key ON to_key.fact_k = r.to_k
                JOIN facts to_fact ON to_fact.object_id = to_key.object_id
                WHERE {anchor_clause}
                  AND r.relation_kind_code IN ({placeholders})
                ORDER BY r.relation_kind_code ASC, f.object_name ASC, f.object_source ASC, r.relative_k ASC
                """,
                params,
            ).fetchall()
        pairs = [
            (_fact_from_index_row(row[0:8]), _relative_from_index_row(row[8:17]))
            for row in rows
        ]
        return _relation_search_from_pairs(spec, anchor, pairs, limit)

    def _call_edges_for_frontier(
        self,
        frontier_ids: Iterable[str],
        direction: str,
    ) -> List[Tuple[str, FactRecord, FactRelative]]:
        direct_call_code = RELATION_KIND_CODES["direct_call"]
        assigned_to_code = RELATION_KIND_CODES["assigned_to"]
        dispatches_via_code = RELATION_KIND_CODES["dispatches_via"]
        ids = list(frontier_ids)
        if not ids:
            return []
        edges: List[Tuple[str, FactRecord, FactRelative]] = []
        source_column = "r.to_k" if direction == "incoming" else "r.from_k"
        endpoint_join = "endpoint_key.fact_k = r.from_k" if direction == "incoming" else "endpoint_key.fact_k = r.to_k"
        source_join = "source_key.fact_k = r.to_k" if direction == "incoming" else "source_key.fact_k = r.from_k"
        with self.lock:
            for offset in range(0, len(ids), 500):
                batch = ids[offset : offset + 500]
                fact_keys = self._fact_keys_for_ids_unlocked(batch)
                if not fact_keys:
                    continue
                frontier_keys = list(fact_keys.values())
                placeholders = ",".join("?" for _ in frontier_keys)
                rows = self.connection.execute(
                    f"""
                    SELECT source_fact.object_id AS source_fact_id,
                           f.object_id, f.object_name, f.object_description, f.object_source,
                           f.object_profile, f.object_caller, f.object_callee, f.payload_json,
                           ri.relative_id, from_fact.object_id, to_fact.object_id, r.relation_kind_code,
                           r.confidence, r.object_profile, r.evidence_source, r.condition_json, r.payload_json
                    FROM relatives r
                    JOIN relative_ids ri ON ri.relative_k = r.relative_k
                    JOIN fact_keys endpoint_key ON {endpoint_join}
                    JOIN facts f ON f.object_id = endpoint_key.object_id
                    JOIN fact_keys source_key ON {source_join}
                    JOIN facts source_fact ON source_fact.object_id = source_key.object_id
                    JOIN fact_keys from_key ON from_key.fact_k = r.from_k
                    JOIN facts from_fact ON from_fact.object_id = from_key.object_id
                    JOIN fact_keys to_key ON to_key.fact_k = r.to_k
                    JOIN facts to_fact ON to_fact.object_id = to_key.object_id
                    WHERE {source_column} IN ({placeholders})
                      AND r.relation_kind_code = ?
                    ORDER BY source_key.fact_k ASC, f.object_name ASC, f.object_source ASC, r.relative_k ASC
                    """,
                    (*frontier_keys, direct_call_code),
                ).fetchall()
                edges.extend(
                    (
                        str(row[0]),
                        _fact_from_index_row(row[1:9]),
                        _relative_from_index_row(row[9:18]),
                    )
                    for row in rows
                )
                if direction == "incoming":
                    dispatch_rows = self.connection.execute(
                        f"""
                        SELECT source_fact.object_id AS source_fact_id,
                               f.object_id, f.object_name, f.object_description, f.object_source,
                               f.object_profile, f.object_caller, f.object_callee, f.payload_json,
                               ri.relative_id, from_fact.object_id, to_fact.object_id, d.relation_kind_code,
                               d.confidence, d.object_profile, d.evidence_source, d.condition_json, d.payload_json
                        FROM relatives a
                        JOIN relatives d ON d.to_k = a.from_k
                        JOIN relative_ids ri ON ri.relative_k = d.relative_k
                        JOIN fact_keys endpoint_key ON endpoint_key.fact_k = d.from_k
                        JOIN facts f ON f.object_id = endpoint_key.object_id
                        JOIN fact_keys source_key ON source_key.fact_k = a.to_k
                        JOIN facts source_fact ON source_fact.object_id = source_key.object_id
                        JOIN fact_keys from_key ON from_key.fact_k = d.from_k
                        JOIN facts from_fact ON from_fact.object_id = from_key.object_id
                        JOIN fact_keys to_key ON to_key.fact_k = d.to_k
                        JOIN facts to_fact ON to_fact.object_id = to_key.object_id
                        WHERE a.to_k IN ({placeholders})
                          AND a.relation_kind_code = ?
                          AND d.relation_kind_code = ?
                        ORDER BY source_key.fact_k ASC, f.object_name ASC, f.object_source ASC, d.relative_k ASC
                        """,
                        (*frontier_keys, assigned_to_code, dispatches_via_code),
                    ).fetchall()
                else:
                    dispatch_rows = self.connection.execute(
                        f"""
                        SELECT source_fact.object_id AS source_fact_id,
                               f.object_id, f.object_name, f.object_description, f.object_source,
                               f.object_profile, f.object_caller, f.object_callee, f.payload_json,
                               ri.relative_id, from_fact.object_id, to_fact.object_id, d.relation_kind_code,
                               d.confidence, d.object_profile, d.evidence_source, d.condition_json, d.payload_json
                        FROM relatives d
                        JOIN relatives a ON a.from_k = d.to_k
                        JOIN relative_ids ri ON ri.relative_k = d.relative_k
                        JOIN fact_keys endpoint_key ON endpoint_key.fact_k = a.to_k
                        JOIN facts f ON f.object_id = endpoint_key.object_id
                        JOIN fact_keys source_key ON source_key.fact_k = d.from_k
                        JOIN facts source_fact ON source_fact.object_id = source_key.object_id
                        JOIN fact_keys from_key ON from_key.fact_k = d.from_k
                        JOIN facts from_fact ON from_fact.object_id = from_key.object_id
                        JOIN fact_keys to_key ON to_key.fact_k = d.to_k
                        JOIN facts to_fact ON to_fact.object_id = to_key.object_id
                        WHERE d.from_k IN ({placeholders})
                          AND d.relation_kind_code = ?
                          AND a.relation_kind_code = ?
                        ORDER BY source_key.fact_k ASC, f.object_name ASC, f.object_source ASC, d.relative_k ASC
                        """,
                        (*frontier_keys, dispatches_via_code, assigned_to_code),
                    ).fetchall()
                edges.extend(
                    (
                        str(row[0]),
                        _fact_from_index_row(row[1:9]),
                        _relative_from_index_row(row[9:18]),
                    )
                    for row in dispatch_rows
                )
        return sorted(
            edges,
            key=lambda item: (item[0], item[1].object_name, item[1].object_source, item[2].relative_id),
        )

    def _relation_anchor_candidates(
        self,
        spec: RelationSearchQuery,
        limit: int,
    ) -> List[RelationSearchAnchorCandidate]:
        candidates: Dict[str, RelationSearchAnchorCandidate] = {}
        exact = self.get_fact(spec.anchor)
        if exact is not None and _relation_anchor_kind_matches(exact, spec.anchor_kind):
            _add_anchor_candidate(candidates, exact, 0, exact.object_name.casefold() == spec.anchor.casefold(), spec.anchor_role)
            return sorted(candidates.values(), key=_anchor_candidate_sort_key)

        candidates = {}
        for fact in self._field_owner_anchor_facts(spec):
            _add_anchor_candidate(candidates, fact, 1, True, spec.anchor_role)
        if candidates:
            return sorted(candidates.values(), key=_anchor_candidate_sort_key)

        candidates = {}
        for fact in self._exact_name_anchor_facts(spec):
            _add_anchor_candidate(candidates, fact, 2, True, spec.anchor_role)
        if candidates:
            return sorted(candidates.values(), key=_anchor_candidate_sort_key)

        candidates = {}
        candidate_limit = _search_candidate_limit(max(limit, SEARCH_EXACT_KIND_FLOOR))
        for fact in self.search(spec.anchor, candidate_limit):
            if _relation_anchor_kind_matches(fact, spec.anchor_kind):
                _add_anchor_candidate(
                    candidates,
                    fact,
                    3,
                    fact.object_name.casefold() == spec.anchor.casefold(),
                    spec.anchor_role,
                )
        return sorted(candidates.values(), key=_anchor_candidate_sort_key)

    def _exact_name_anchor_facts(self, spec: RelationSearchQuery) -> List[FactRecord]:
        key = spec.anchor.casefold()
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT object_id, object_name, object_description, object_source,
                       object_profile, object_caller, object_callee, payload_json
                FROM facts
                WHERE lower(object_name) = ? OR (object_name_cf IS NOT NULL AND object_name_cf = ?)
                ORDER BY object_source ASC, object_id ASC
                """,
                (key, key),
            ).fetchall()
        return [
            fact
            for fact in (_fact_from_index_row(row) for row in rows)
            if _relation_anchor_kind_matches(fact, spec.anchor_kind)
        ]

    def _field_owner_anchor_facts(self, spec: RelationSearchQuery) -> List[FactRecord]:
        if spec.anchor_kind != "field":
            return []
        rows: List[Tuple[Any, ...]] = []
        with self.lock:
            for owner_key, field_key in _owner_field_query_pairs(spec.anchor):
                rows.extend(
                    self.connection.execute(
                        """
                        SELECT object_id, object_name, object_description, object_source,
                               object_profile, object_caller, object_callee, payload_json
                        FROM facts
                        WHERE (lower(object_name) = ? OR (object_name_cf IS NOT NULL AND object_name_cf = ?))
                          AND instr(lower(payload_json), ?) > 0
                        ORDER BY object_source ASC, object_id ASC
                        """,
                        (field_key, field_key, owner_key),
                    ).fetchall()
                )
        return [
            fact
            for fact in (_fact_from_index_row(row) for row in rows)
            if _relation_anchor_kind_matches(fact, spec.anchor_kind)
        ]


_READ_INDEX_CACHE: "OrderedDict[Tuple[Any, ...], _ReadIndex]" = OrderedDict()
_READ_INDEX_CACHE_LOCK = threading.RLock()

def _fact_index_tuple(fact: FactRecord) -> Tuple[Any, ...]:
    return (
        fact.object_id,
        fact.object_name,
        fact.object_description,
        fact.object_source,
        fact.object_profile,
        fact.object_caller,
        fact.object_callee,
        _json_text(fact.payload),
        _fact_kind_search_rank(_fact_kind(fact)),
        _unicode_casefold_fallback(fact.object_name),
        _unicode_casefold_fallback(fact.object_description),
        _unicode_casefold_fallback(fact.object_caller),
        _unicode_casefold_fallback(fact.object_callee),
        _unicode_casefold_fallback(fact.object_source),
    )


def _relative_index_tuple(relative_k: int, relative: FactRelative, from_k: int, to_k: int) -> Tuple[Any, ...]:
    return (
        relative_k,
        from_k,
        to_k,
        RELATION_KIND_CODES[relative.relation_kind],
        float(relative.confidence),
        None if relative.object_profile == "default" else relative.object_profile,
        relative.evidence_source,
        _json_text(relative.condition.to_json()) if relative.condition is not None else None,
        _json_text(relative.payload),
    )


def _fact_from_index_row(row: Tuple[Any, ...]) -> FactRecord:
    payload = _json_from_text(row[7], "fact payload")
    if not isinstance(payload, dict):
        raise StorageError("snapshot_corrupt", "fact index payload must be a JSON object")
    return FactRecord(
        object_id=row[0],
        object_name=row[1],
        object_description=row[2],
        object_source=row[3],
        object_profile=row[4],
        object_caller=row[5],
        object_callee=row[6],
        payload=payload,
    )


def _relative_from_index_row(row: Tuple[Any, ...]) -> FactRelative:
    relation_kind = RELATION_KIND_BY_CODE.get(row[3])
    if relation_kind is None:
        raise StorageError("snapshot_corrupt", "relative index has unknown relation kind code")
    condition_payload = _json_from_text(row[7], "relative condition") if row[7] is not None else None
    if condition_payload is not None and not isinstance(condition_payload, dict):
        raise StorageError("snapshot_corrupt", "relative condition payload must be a JSON object")
    payload = _json_from_text(row[8], "relative payload")
    if not isinstance(payload, dict):
        raise StorageError("snapshot_corrupt", "relative index payload must be a JSON object")
    return FactRelative(
        relative_id=row[0],
        from_fact_id=row[1],
        to_fact_id=row[2],
        relation_kind=relation_kind,
        confidence=row[4],
        object_profile=row[5] if row[5] is not None else "default",
        evidence_source=row[6],
        condition=RelativeCondition.from_json(condition_payload),
        payload=payload,
    )

def _insert_persistent_fact_index_batch(connection: sqlite3.Connection, batch: List[Tuple[Any, ...]]) -> None:
    try:
        connection.executemany(
            """
            INSERT INTO facts(
                object_id,
                object_name,
                object_description,
                object_source,
                object_profile,
                object_caller,
                object_callee,
                payload_json,
                fact_kind_rank,
                object_name_cf,
                object_description_cf,
                object_caller_cf,
                object_callee_cf,
                object_source_cf
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
    except sqlite3.IntegrityError as exc:
        raise StorageError("duplicate_object_id", "duplicate object_id") from exc


def _insert_persistent_fact_key_batch(connection: sqlite3.Connection, batch: List[Tuple[int, str]]) -> None:
    try:
        connection.executemany(
            "INSERT INTO fact_keys(fact_k, object_id) VALUES (?, ?)",
            batch,
        )
    except sqlite3.IntegrityError as exc:
        raise StorageError("duplicate_object_id", "duplicate object_id") from exc


def _insert_persistent_relative_index_batch(connection: sqlite3.Connection, batch: List[Tuple[int, FactRelative]]) -> None:
    fact_keys = _read_fact_keys_for_relative_batch(connection, batch)
    relative_id_batch: List[Tuple[Any, ...]] = []
    relative_batch: List[Tuple[Any, ...]] = []
    for relative_k, relative in batch:
        from_k = fact_keys.get(relative.from_fact_id)
        to_k = fact_keys.get(relative.to_fact_id)
        if from_k is None or to_k is None:
            raise StorageError("relative_endpoint_missing", "relative endpoint is missing")
        relative_id_batch.append((relative_k, relative.relative_id))
        relative_batch.append(_relative_index_tuple(relative_k, relative, from_k, to_k))
    try:
        connection.executemany(
            "INSERT INTO relative_ids(relative_k, relative_id) VALUES (?, ?)",
            relative_id_batch,
        )
        connection.executemany(
            """
            INSERT INTO relatives(
                relative_k,
                from_k,
                to_k,
                relation_kind_code,
                confidence,
                object_profile,
                evidence_source,
                condition_json,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            relative_batch,
        )
    except sqlite3.IntegrityError as exc:
        raise StorageError("duplicate_relative_id", "duplicate relative_id") from exc


def _read_fact_keys_for_relative_batch(
    connection: sqlite3.Connection,
    batch: List[Tuple[int, FactRelative]],
) -> Dict[str, int]:
    object_ids = sorted({relative.from_fact_id for _, relative in batch} | {relative.to_fact_id for _, relative in batch})
    if not object_ids:
        return {}
    output: Dict[str, int] = {}
    for offset in range(0, len(object_ids), 500):
        chunk = object_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT object_id, fact_k FROM fact_keys WHERE object_id IN ({placeholders})",
            chunk,
        ).fetchall()
        output.update((str(row[0]), int(row[1])) for row in rows)
    return output

__all__ = [name for name in globals() if not name.startswith("__")]

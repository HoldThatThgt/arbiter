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

from ._common import JSONValue
from ._common import LogError, LogEvent, open_log

from .constants import *
from .utils import *

class _FrozenSlots:
    __slots__ = ("_frozen",)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_frozen", False):
            raise FrozenInstanceError(f"cannot assign to field {name!r}")
        object.__setattr__(self, name, value)

    def _freeze(self) -> None:
        object.__setattr__(self, "_frozen", True)
EXACT_NAME_SEARCH_BONUS = 10
SEARCH_EXACT_KIND_FLOOR = 3
SEARCH_CANDIDATE_MIN = 100
SEARCH_CANDIDATE_MULTIPLIER = 8
FIELD_OWNER_SEARCH_WEIGHT = 1
_FIELD_QUERY_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASCII_LOWER_TRANSLATION = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
RELATION_SEARCH_DEFINITIONS = {
    "readers": ("field", "incoming", ("field_read",)),
    "writers": ("field", "incoming", ("field_write",)),
    "accessors": ("field", "incoming", ("field_write", "field_read")),
    "callers": ("function", "incoming", ("direct_call",)),
    "callees": ("function", "outgoing", ("direct_call",)),
    "dispatches_via": ("field", "outgoing", ("assigned_to",)),
}
RELATION_SEARCH_SALIENCE_RANKS = {
    "direct_call": 0,
    "dispatches_via": 0,
    "field_write": 1,
    "field_read": 2,
    "assigned_to": 3,
}
RELATION_CLOSURE_MAX_DEPTH = 3
RELATION_REACHABLE_MAX_DEPTH = 8
RELATION_TRANSITIVE_VISITED_BUDGET = 10_000
RELATION_TRANSITIVE_FRONTIER_BUDGET = 50_000


class StorageError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: Optional[Path] = None,
        details: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = str(path) if path is not None else None
        self.details = dict(details or {})


class FactRecord(_FrozenSlots):
    __slots__ = (
        "object_id",
        "object_name",
        "object_description",
        "object_source",
        "object_profile",
        "object_caller",
        "object_callee",
        "payload",
    )

    def __init__(
        self,
        object_id: str,
        object_name: str,
        object_description: str,
        object_source: str,
        object_profile: str,
        object_caller: Optional[str] = None,
        object_callee: Optional[str] = None,
        payload: Any = _MISSING,
    ) -> None:
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "object_name", object_name)
        object.__setattr__(self, "object_description", object_description)
        object.__setattr__(self, "object_source", object_source)
        object.__setattr__(self, "object_profile", object_profile)
        object.__setattr__(self, "object_caller", object_caller)
        object.__setattr__(self, "object_callee", object_callee)
        object.__setattr__(self, "payload", {} if payload is _MISSING else payload)
        self.__post_init__()
        self._freeze()

    def __repr__(self) -> str:
        return (
            "FactRecord("
            f"object_id={self.object_id!r}, "
            f"object_name={self.object_name!r}, "
            f"object_description={self.object_description!r}, "
            f"object_source={self.object_source!r}, "
            f"object_profile={self.object_profile!r}, "
            f"object_caller={self.object_caller!r}, "
            f"object_callee={self.object_callee!r}, "
            f"payload={self.payload!r})"
        )

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return False
        return (
            self.object_id,
            self.object_name,
            self.object_description,
            self.object_source,
            self.object_profile,
            self.object_caller,
            self.object_callee,
            self.payload,
        ) == (
            other.object_id,
            other.object_name,
            other.object_description,
            other.object_source,
            other.object_profile,
            other.object_caller,
            other.object_callee,
            other.payload,
        )

    __hash__ = None  # type: ignore[assignment]

    def __getstate__(self) -> Tuple[Any, ...]:
        return (
            self.object_id,
            self.object_name,
            self.object_description,
            self.object_source,
            self.object_profile,
            self.object_caller,
            self.object_callee,
            self.payload,
        )

    def __setstate__(self, state: Tuple[Any, ...]) -> None:
        for name, value in zip(self.__slots__, state):
            object.__setattr__(self, name, value)
        self._freeze()

    def __post_init__(self) -> None:
        for field_name in (
            "object_id",
            "object_name",
            "object_description",
            "object_source",
            "object_profile",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise StorageError("invalid_fact", f"{field_name} must be a non-empty string")
        for field_name in ("object_caller", "object_callee"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise StorageError("invalid_fact", f"{field_name} must be a string or None")
        if not isinstance(self.payload, dict):
            raise StorageError("invalid_fact", "payload must be a JSON object")
        _ensure_json_value(self.payload)
        payload_size = len(_canonical_json(self.to_payload()).encode("utf-8"))
        if payload_size > MAX_FACT_PAYLOAD_BYTES:
            raise StorageError("payload_too_large", "single fact payload exceeds 4KB")

    def to_json(self) -> Dict[str, Any]:
        return {
            "object_id": self.object_id,
            "object_name": self.object_name,
            "object_description": self.object_description,
            "object_source": self.object_source,
            "object_profile": self.object_profile,
            "object_caller": self.object_caller,
            "object_callee": self.object_callee,
            "payload": dict(self.payload),
        }

    def to_payload(self) -> Dict[str, JSONValue]:
        payload: Dict[str, JSONValue] = dict(self.payload)
        payload.update(
            {
                "object_id": self.object_id,
                "object_name": self.object_name,
                "object_description": self.object_description,
                "object_source": self.object_source,
                "object_profile": self.object_profile,
                "object_caller": self.object_caller,
                "object_callee": self.object_callee,
            }
        )
        return payload

    @classmethod
    def from_json(cls, row: Dict[str, Any]) -> "FactRecord":
        if not isinstance(row, dict):
            raise StorageError("invalid_fact", "fact row must be a JSON object")
        if "payload" in row:
            return cls(
                object_id=row.get("object_id"),
                object_name=row.get("object_name"),
                object_description=row.get("object_description"),
                object_source=row.get("object_source"),
                object_profile=row.get("object_profile"),
                object_caller=row.get("object_caller"),
                object_callee=row.get("object_callee"),
                payload=row.get("payload") or {},
            )
        known = {
            "object_id",
            "object_name",
            "object_description",
            "object_source",
            "object_profile",
            "object_caller",
            "object_callee",
        }
        return cls(
            object_id=row.get("object_id"),
            object_name=row.get("object_name"),
            object_description=row.get("object_description"),
            object_source=row.get("object_source"),
            object_profile=row.get("object_profile"),
            object_caller=row.get("object_caller"),
            object_callee=row.get("object_callee"),
            payload={key: value for key, value in row.items() if key not in known},
        )


@dataclass(frozen=True)
class StoredFactLine:
    schema_version: int
    object_id: str
    fact_kind: str
    payload: Dict[str, JSONValue]
    payload_sha256: str

    @classmethod
    def from_fact(cls, fact: FactRecord) -> "StoredFactLine":
        payload = fact.to_payload()
        fact_kind = payload.get("fact_kind")
        if not isinstance(fact_kind, str) or not fact_kind:
            fact_kind = "fact"
        return cls(
            schema_version=SCHEMA_VERSION,
            object_id=fact.object_id,
            fact_kind=fact_kind,
            payload=payload,
            payload_sha256=_sha256_text(_canonical_json(payload)),
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "object_id": self.object_id,
            "fact_kind": self.fact_kind,
            "payload": dict(self.payload),
            "payload_sha256": self.payload_sha256,
        }

    @classmethod
    def from_json(cls, row: Dict[str, Any]) -> "StoredFactLine":
        if not isinstance(row, dict):
            raise StorageError("snapshot_corrupt", "fact line must be a JSON object")
        if row.get("schema_version") != SCHEMA_VERSION:
            raise StorageError("unsupported_schema_version", "unsupported fact line schema version")
        for field_name in ("object_id", "fact_kind", "payload", "payload_sha256"):
            if field_name not in row:
                raise StorageError("snapshot_corrupt", f"fact line missing {field_name}")
        if not isinstance(row["payload"], dict):
            raise StorageError("snapshot_corrupt", "fact line payload must be an object")
        expected = _sha256_text(_canonical_json(row["payload"]))
        if row["payload_sha256"] != expected:
            raise StorageError("snapshot_corrupt", "fact payload hash mismatch")
        return cls(
            schema_version=row["schema_version"],
            object_id=row["object_id"],
            fact_kind=row["fact_kind"],
            payload=row["payload"],
            payload_sha256=row["payload_sha256"],
        )

    def to_fact(self) -> FactRecord:
        return FactRecord.from_json(self.payload)


@dataclass(frozen=True)
class RelativeCondition:
    kind: str
    expression: Optional[str] = None
    branch: Optional[str] = None
    source: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in CONDITION_KINDS:
            raise StorageError("invalid_condition", "condition kind is not supported")
        for field_name in ("expression", "branch", "source"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise StorageError("invalid_condition", f"{field_name} must be a string or None")
        if len(_canonical_json(self.to_json()).encode("utf-8")) > MAX_CONDITION_BYTES:
            raise StorageError("condition_too_large", "relative condition exceeds 1KB")

    def to_json(self) -> Dict[str, Optional[str]]:
        return {
            "kind": self.kind,
            "expression": self.expression,
            "branch": self.branch,
            "source": self.source,
        }

    @classmethod
    def from_json(cls, row: Optional[Dict[str, Any]]) -> Optional["RelativeCondition"]:
        if row is None:
            return None
        if not isinstance(row, dict):
            raise StorageError("invalid_condition", "condition must be an object or None")
        allowed = {"kind", "expression", "branch", "source"}
        if any(key not in allowed for key in row):
            raise StorageError("invalid_condition", "condition contains unsupported fields")
        return cls(
            kind=row.get("kind"),
            expression=row.get("expression"),
            branch=row.get("branch"),
            source=row.get("source"),
        )


class FactRelative(_FrozenSlots):
    __slots__ = (
        "relative_id",
        "from_fact_id",
        "to_fact_id",
        "relation_kind",
        "condition",
        "object_profile",
        "evidence_source",
        "confidence",
        "payload",
    )

    def __init__(
        self,
        relative_id: str,
        from_fact_id: str,
        to_fact_id: str,
        relation_kind: str,
        condition: Optional[RelativeCondition],
        object_profile: str,
        evidence_source: str,
        confidence: float,
        payload: Any = _MISSING,
    ) -> None:
        object.__setattr__(self, "relative_id", relative_id)
        object.__setattr__(self, "from_fact_id", from_fact_id)
        object.__setattr__(self, "to_fact_id", to_fact_id)
        object.__setattr__(self, "relation_kind", relation_kind)
        object.__setattr__(self, "condition", condition)
        object.__setattr__(self, "object_profile", object_profile)
        object.__setattr__(self, "evidence_source", evidence_source)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "payload", {} if payload is _MISSING else payload)
        self.__post_init__()
        self._freeze()

    def __repr__(self) -> str:
        return (
            "FactRelative("
            f"relative_id={self.relative_id!r}, "
            f"from_fact_id={self.from_fact_id!r}, "
            f"to_fact_id={self.to_fact_id!r}, "
            f"relation_kind={self.relation_kind!r}, "
            f"condition={self.condition!r}, "
            f"object_profile={self.object_profile!r}, "
            f"evidence_source={self.evidence_source!r}, "
            f"confidence={self.confidence!r}, "
            f"payload={self.payload!r})"
        )

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return False
        return (
            self.relative_id,
            self.from_fact_id,
            self.to_fact_id,
            self.relation_kind,
            self.condition,
            self.object_profile,
            self.evidence_source,
            self.confidence,
            self.payload,
        ) == (
            other.relative_id,
            other.from_fact_id,
            other.to_fact_id,
            other.relation_kind,
            other.condition,
            other.object_profile,
            other.evidence_source,
            other.confidence,
            other.payload,
        )

    __hash__ = None  # type: ignore[assignment]

    def __getstate__(self) -> Tuple[Any, ...]:
        return (
            self.relative_id,
            self.from_fact_id,
            self.to_fact_id,
            self.relation_kind,
            self.condition,
            self.object_profile,
            self.evidence_source,
            self.confidence,
            self.payload,
        )

    def __setstate__(self, state: Tuple[Any, ...]) -> None:
        for name, value in zip(self.__slots__, state):
            object.__setattr__(self, name, value)
        self._freeze()

    def __post_init__(self) -> None:
        for field_name in ("relative_id", "from_fact_id", "to_fact_id", "object_profile", "evidence_source"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise StorageError("invalid_relative", f"{field_name} must be a non-empty string")
        if self.relation_kind not in RELATION_KINDS:
            raise StorageError("invalid_relation_kind", unsupported_relation_kind_message(self.relation_kind))
        if self.condition is not None and not isinstance(self.condition, RelativeCondition):
            raise StorageError("invalid_condition", "condition must be RelativeCondition or None")
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool) or not math.isfinite(float(self.confidence)):
            raise StorageError("invalid_relative", "confidence must be a finite number")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise StorageError("invalid_relative", "confidence must be between 0.0 and 1.0")
        if not isinstance(self.payload, dict):
            raise StorageError("invalid_relative", "payload must be a JSON object")
        _ensure_json_value(self.payload, code="invalid_relative")
        payload_size = len(_canonical_json(self.to_payload()).encode("utf-8"))
        if payload_size > MAX_RELATIVE_PAYLOAD_BYTES:
            raise StorageError("payload_too_large", "single relative payload exceeds 2KB")

    def to_json(self) -> Dict[str, Any]:
        return {
            "relative_id": self.relative_id,
            "from_fact_id": self.from_fact_id,
            "to_fact_id": self.to_fact_id,
            "relation_kind": self.relation_kind,
            "condition": self.condition.to_json() if self.condition is not None else None,
            "object_profile": self.object_profile,
            "evidence_source": self.evidence_source,
            "confidence": float(self.confidence),
            "payload": dict(self.payload),
        }

    def to_payload(self) -> Dict[str, JSONValue]:
        payload: Dict[str, JSONValue] = dict(self.payload)
        payload.update(
            {
                "relative_id": self.relative_id,
                "from_fact_id": self.from_fact_id,
                "to_fact_id": self.to_fact_id,
                "relation_kind": self.relation_kind,
                "condition": self.condition.to_json() if self.condition is not None else None,
                "object_profile": self.object_profile,
                "evidence_source": self.evidence_source,
                "confidence": float(self.confidence),
            }
        )
        return payload

    @classmethod
    def from_json(cls, row: Dict[str, Any]) -> "FactRelative":
        if not isinstance(row, dict):
            raise StorageError("invalid_relative", "relative row must be a JSON object")
        if "payload" in row:
            return cls(
                relative_id=row.get("relative_id"),
                from_fact_id=row.get("from_fact_id"),
                to_fact_id=row.get("to_fact_id"),
                relation_kind=row.get("relation_kind"),
                condition=RelativeCondition.from_json(row.get("condition")),
                object_profile=row.get("object_profile"),
                evidence_source=row.get("evidence_source"),
                confidence=row.get("confidence"),
                payload=row.get("payload") or {},
            )
        known = {
            "relative_id",
            "from_fact_id",
            "to_fact_id",
            "relation_kind",
            "condition",
            "object_profile",
            "evidence_source",
            "confidence",
        }
        return cls(
            relative_id=row.get("relative_id"),
            from_fact_id=row.get("from_fact_id"),
            to_fact_id=row.get("to_fact_id"),
            relation_kind=row.get("relation_kind"),
            condition=RelativeCondition.from_json(row.get("condition")),
            object_profile=row.get("object_profile"),
            evidence_source=row.get("evidence_source"),
            confidence=row.get("confidence"),
            payload={key: value for key, value in row.items() if key not in known},
        )


@dataclass(frozen=True)
class StoredRelativeLine:
    schema_version: int
    relative_id: str
    from_fact_id: str
    to_fact_id: str
    relation_kind: str
    condition: Optional[Dict[str, JSONValue]]
    payload: Dict[str, JSONValue]
    payload_sha256: str

    @classmethod
    def from_relative(cls, relative: FactRelative) -> "StoredRelativeLine":
        payload = relative.to_payload()
        return cls(
            schema_version=SCHEMA_VERSION,
            relative_id=relative.relative_id,
            from_fact_id=relative.from_fact_id,
            to_fact_id=relative.to_fact_id,
            relation_kind=relative.relation_kind,
            condition=relative.condition.to_json() if relative.condition is not None else None,
            payload=payload,
            payload_sha256=_sha256_text(_canonical_json(payload)),
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "relative_id": self.relative_id,
            "from_fact_id": self.from_fact_id,
            "to_fact_id": self.to_fact_id,
            "relation_kind": self.relation_kind,
            "condition": self.condition,
            "payload": dict(self.payload),
            "payload_sha256": self.payload_sha256,
        }

    @classmethod
    def from_json(cls, row: Dict[str, Any]) -> "StoredRelativeLine":
        if not isinstance(row, dict):
            raise StorageError("snapshot_corrupt", "relative line must be a JSON object")
        if row.get("schema_version") != SCHEMA_VERSION:
            raise StorageError("unsupported_schema_version", "unsupported relative line schema version")
        for field_name in (
            "relative_id",
            "from_fact_id",
            "to_fact_id",
            "relation_kind",
            "condition",
            "payload",
            "payload_sha256",
        ):
            if field_name not in row:
                raise StorageError("snapshot_corrupt", f"relative line missing {field_name}")
        if not isinstance(row["payload"], dict):
            raise StorageError("snapshot_corrupt", "relative line payload must be an object")
        expected = _sha256_text(_canonical_json(row["payload"]))
        if row["payload_sha256"] != expected:
            raise StorageError("snapshot_corrupt", "relative payload hash mismatch")
        return cls(
            schema_version=row["schema_version"],
            relative_id=row["relative_id"],
            from_fact_id=row["from_fact_id"],
            to_fact_id=row["to_fact_id"],
            relation_kind=row["relation_kind"],
            condition=row["condition"],
            payload=row["payload"],
            payload_sha256=row["payload_sha256"],
        )

    def to_relative(self) -> FactRelative:
        return FactRelative.from_json(self.payload)


@dataclass(frozen=True)
class EncodedFactLine:
    object_id: str
    fact_kind: str
    object_name: str
    object_source: str
    object_profile: str
    object_caller: Optional[str]
    object_callee: Optional[str]
    canonical_source: Optional[str] = None
    linkage: Optional[str] = None
    line_text: Optional[str] = None
    line_path: Optional[Path] = None
    byte_offset: int = 0
    byte_length: Optional[int] = None

    @classmethod
    def from_fact(cls, fact: FactRecord) -> "EncodedFactLine":
        return cls.from_fact_fields(
            object_id=fact.object_id,
            object_name=fact.object_name,
            object_description=fact.object_description,
            object_source=fact.object_source,
            object_profile=fact.object_profile,
            object_caller=fact.object_caller,
            object_callee=fact.object_callee,
            fact_kind=fact.payload.get("fact_kind") if isinstance(fact.payload.get("fact_kind"), str) else "fact",
            payload=fact.payload,
        )

    @classmethod
    def from_fact_fields(
        cls,
        *,
        object_id: str,
        object_name: str,
        object_description: str,
        object_source: str,
        object_profile: str,
        object_caller: Optional[str],
        object_callee: Optional[str],
        fact_kind: str,
        payload: Dict[str, JSONValue],
    ) -> "EncodedFactLine":
        from .serialization import _validate_fact_fields

        _validate_fact_fields(
            object_id=object_id,
            object_name=object_name,
            object_description=object_description,
            object_source=object_source,
            object_profile=object_profile,
            object_caller=object_caller,
            object_callee=object_callee,
            payload=payload,
        )
        full_payload: Dict[str, JSONValue] = dict(payload)
        full_payload.update(
            {
                "object_id": object_id,
                "object_name": object_name,
                "object_description": object_description,
                "object_source": object_source,
                "object_profile": object_profile,
                "object_caller": object_caller,
                "object_callee": object_callee,
            }
        )
        payload_text = _canonical_json(full_payload)
        if len(payload_text.encode("utf-8")) > MAX_FACT_PAYLOAD_BYTES:
            raise StorageError("payload_too_large", "single fact payload exceeds 4KB")
        line = StoredFactLine(
            schema_version=SCHEMA_VERSION,
            object_id=object_id,
            fact_kind=fact_kind if fact_kind else "fact",
            payload=full_payload,
            payload_sha256=_sha256_text(payload_text),
        )
        return cls.from_stored_line(line, line_text=_canonical_json(line.to_json()) + "\n")

    @classmethod
    def from_stored_line(cls, line: StoredFactLine, *, line_text: str) -> "EncodedFactLine":
        payload = line.payload
        canonical_source = payload.get("canonical_source")
        linkage = payload.get("linkage")
        return cls(
            object_id=line.object_id,
            fact_kind=line.fact_kind,
            object_name=str(payload.get("object_name")),
            object_source=str(payload.get("object_source")),
            object_profile=str(payload.get("object_profile")),
            object_caller=payload.get("object_caller") if isinstance(payload.get("object_caller"), str) else None,
            object_callee=payload.get("object_callee") if isinstance(payload.get("object_callee"), str) else None,
            canonical_source=canonical_source if isinstance(canonical_source, str) and canonical_source else None,
            linkage=linkage if isinstance(linkage, str) and linkage else None,
            line_text=line_text,
        )

    def read_line_text(self) -> str:
        if self.line_text is not None:
            return self.line_text
        if self.line_path is None or self.byte_length is None:
            raise StorageError("snapshot_corrupt", "encoded fact line is missing line text")
        with self.line_path.open("rb") as handle:
            handle.seek(self.byte_offset)
            raw = handle.read(self.byte_length)
        return raw.decode("utf-8")


@dataclass(frozen=True)
class EncodedRelativeLine:
    relative_id: str
    from_fact_id: str
    to_fact_id: str
    relation_kind: str
    condition: Optional[Dict[str, JSONValue]]
    object_profile: str
    line_text: Optional[str] = None
    line_path: Optional[Path] = None
    byte_offset: int = 0
    byte_length: Optional[int] = None

    @classmethod
    def from_relative(cls, relative: FactRelative) -> "EncodedRelativeLine":
        return cls.from_relative_fields(
            relative_id=relative.relative_id,
            from_fact_id=relative.from_fact_id,
            to_fact_id=relative.to_fact_id,
            relation_kind=relative.relation_kind,
            condition=relative.condition.to_json() if relative.condition is not None else None,
            object_profile=relative.object_profile,
            evidence_source=relative.evidence_source,
            confidence=float(relative.confidence),
            payload=relative.payload,
        )

    @classmethod
    def from_relative_fields(
        cls,
        *,
        relative_id: str,
        from_fact_id: str,
        to_fact_id: str,
        relation_kind: str,
        condition: Optional[Dict[str, JSONValue]],
        object_profile: str,
        evidence_source: str,
        confidence: float,
        payload: Dict[str, JSONValue],
    ) -> "EncodedRelativeLine":
        from .serialization import _validate_relative_fields

        _validate_relative_fields(
            relative_id=relative_id,
            from_fact_id=from_fact_id,
            to_fact_id=to_fact_id,
            relation_kind=relation_kind,
            condition=condition,
            object_profile=object_profile,
            evidence_source=evidence_source,
            confidence=confidence,
            payload=payload,
        )
        full_payload: Dict[str, JSONValue] = dict(payload)
        full_payload.update(
            {
                "relative_id": relative_id,
                "from_fact_id": from_fact_id,
                "to_fact_id": to_fact_id,
                "relation_kind": relation_kind,
                "condition": condition,
                "object_profile": object_profile,
                "evidence_source": evidence_source,
                "confidence": float(confidence),
            }
        )
        payload_text = _canonical_json(full_payload)
        if len(payload_text.encode("utf-8")) > MAX_RELATIVE_PAYLOAD_BYTES:
            raise StorageError("payload_too_large", "single relative payload exceeds 2KB")
        line = StoredRelativeLine(
            schema_version=SCHEMA_VERSION,
            relative_id=relative_id,
            from_fact_id=from_fact_id,
            to_fact_id=to_fact_id,
            relation_kind=relation_kind,
            condition=condition,
            payload=full_payload,
            payload_sha256=_sha256_text(payload_text),
        )
        return cls.from_stored_line(line, line_text=_canonical_json(line.to_json()) + "\n")

    @classmethod
    def from_stored_line(cls, line: StoredRelativeLine, *, line_text: str) -> "EncodedRelativeLine":
        payload = line.payload
        return cls(
            relative_id=line.relative_id,
            from_fact_id=line.from_fact_id,
            to_fact_id=line.to_fact_id,
            relation_kind=line.relation_kind,
            condition=line.condition,
            object_profile=str(payload.get("object_profile")),
            line_text=line_text,
        )

    def read_line_text(self) -> str:
        if self.line_text is not None:
            return self.line_text
        if self.line_path is None or self.byte_length is None:
            raise StorageError("snapshot_corrupt", "encoded relative line is missing line text")
        with self.line_path.open("rb") as handle:
            handle.seek(self.byte_offset)
            raw = handle.read(self.byte_length)
        return raw.decode("utf-8")


@dataclass(frozen=True)
class SourceInventoryEntry:
    source_id: str
    rel_path: str
    source_kind: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    compile_command_hash: Optional[str]
    toolchain_hash: str
    included_by: List[str] = field(default_factory=list)
    includes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in ("source_id", "rel_path", "source_kind", "sha256", "toolchain_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise StorageError("invalid_source_inventory", f"{field_name} must be a non-empty string")
        if self.rel_path.startswith("/") or any(part == ".." for part in Path(self.rel_path).parts):
            raise StorageError("path_escape", "source inventory rel_path must stay inside target repository")
        if self.source_kind not in {"c_source", "header", "other"}:
            raise StorageError("invalid_source_inventory", "source_kind is not supported")
        if not _is_sha256(self.sha256) or not _is_sha256(self.toolchain_hash):
            raise StorageError("invalid_source_inventory", "sha256 and toolchain_hash must be SHA-256 hex strings")
        if self.compile_command_hash is not None and not _is_sha256(self.compile_command_hash):
            raise StorageError("invalid_source_inventory", "compile_command_hash must be SHA-256 hex or None")
        for field_name in ("size_bytes", "mtime_ns"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StorageError("invalid_source_inventory", f"{field_name} must be a non-negative integer")
        for field_name in ("included_by", "includes"):
            value = getattr(self, field_name)
            if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
                raise StorageError("invalid_source_inventory", f"{field_name} must be a list of source ids")

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "source_id": self.source_id,
            "rel_path": self.rel_path,
            "source_kind": self.source_kind,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "compile_command_hash": self.compile_command_hash,
            "toolchain_hash": self.toolchain_hash,
            "included_by": list(self.included_by),
            "includes": list(self.includes),
        }

    @classmethod
    def from_json(cls, row: Dict[str, Any]) -> "SourceInventoryEntry":
        if not isinstance(row, dict):
            raise StorageError("invalid_source_inventory", "source inventory row must be a JSON object")
        return cls(
            source_id=row.get("source_id"),
            rel_path=row.get("rel_path"),
            source_kind=row.get("source_kind"),
            sha256=row.get("sha256"),
            size_bytes=row.get("size_bytes"),
            mtime_ns=row.get("mtime_ns"),
            compile_command_hash=row.get("compile_command_hash"),
            toolchain_hash=row.get("toolchain_hash"),
            included_by=list(row.get("included_by") or []),
            includes=list(row.get("includes") or []),
        )


@dataclass(frozen=True)
class StoredSourceInventoryLine:
    schema_version: int
    source_id: str
    payload: Dict[str, JSONValue]
    payload_sha256: str

    @classmethod
    def from_entry(cls, entry: SourceInventoryEntry) -> "StoredSourceInventoryLine":
        payload = entry.to_json()
        return cls(
            schema_version=SCHEMA_VERSION,
            source_id=entry.source_id,
            payload=payload,
            payload_sha256=_sha256_text(_canonical_json(payload)),
        )

    def to_json(self) -> Dict[str, JSONValue]:
        payload = dict(self.payload)
        payload.update(
            {
                "schema_version": self.schema_version,
                "payload_sha256": self.payload_sha256,
            }
        )
        return payload

    @classmethod
    def from_json(cls, row: Dict[str, Any]) -> "StoredSourceInventoryLine":
        if not isinstance(row, dict):
            raise StorageError("snapshot_corrupt", "source inventory line must be a JSON object")
        if row.get("schema_version") != SCHEMA_VERSION:
            raise StorageError("unsupported_schema_version", "unsupported source inventory schema version")
        if "payload_sha256" not in row:
            raise StorageError("snapshot_corrupt", "source inventory line missing payload_sha256")
        payload = {key: value for key, value in row.items() if key not in {"schema_version", "payload_sha256"}}
        expected = _sha256_text(_canonical_json(payload))
        if row["payload_sha256"] != expected:
            raise StorageError("snapshot_corrupt", "source inventory payload hash mismatch")
        entry = SourceInventoryEntry.from_json(payload)
        return cls(
            schema_version=row["schema_version"],
            source_id=entry.source_id,
            payload=payload,
            payload_sha256=row["payload_sha256"],
        )

    def to_entry(self) -> SourceInventoryEntry:
        return SourceInventoryEntry.from_json(self.payload)

__all__ = [name for name in globals() if not name.startswith("__")]

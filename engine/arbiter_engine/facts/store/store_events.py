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
from .models import *
from .views import *
from .serialization import _counts_from_payload
from .utils import *

def _persist_log_degradation(
    self,
    snapshot_dir: Path,
    manifest: StorageManifest,
    failures: int,
    latest: Optional[str],
) -> StorageManifest:
    stats = StorageStats.from_json(manifest.stats)
    stats = replace(
        stats,
        log_write_failures=stats.log_write_failures + failures,
        latest_log_error_code=latest,
    )
    degraded = replace(
        manifest,
        log_write_failures=stats.log_write_failures,
        latest_log_error_code=stats.latest_log_error_code,
        stats=stats.to_json(),
    )
    return self._write_manifest_and_stats(snapshot_dir, degraded)

def _emit_relation_search_event(
    self,
    result: RelationSearchResult,
    query: str,
    limit: int,
    started: str,
) -> None:
    filters = len(result.query.file_filters) + len(result.query.name_filters) + len(result.query.terms)
    self._emit_event(
        "storage.search",
        status="ok",
        operation="search",
        outcome=result.status,
        started=started,
        matched_count=result.total,
        limit=limit,
        term_count=len(result.query.terms),
        query_kind=result.query_kind,
        query_preview=query[:80],
        relation_predicate=result.query.predicate,
        anchor_candidate_count=len(result.anchor_candidates),
        matched_endpoint_count=result.matched_endpoint_count if result.matched_endpoint_count is not None else result.total,
        total_is_exact=result.total_is_exact,
        returned_count=len(result.matches),
        too_broad_count=1 if result.status == "too_broad" else 0,
        filter_count=filters,
        complete_count=1 if result.complete else 0,
        depth_requested=result.depth_requested,
        depth_used=result.depth_used,
        depth_max=result.depth_max,
        visited_function_budget=RELATION_TRANSITIVE_VISITED_BUDGET,
        frontier_edge_budget=RELATION_TRANSITIVE_FRONTIER_BUDGET,
        budget_exhausted=result.budget_exhausted,
        budget_exhausted_kind=result.budget_exhausted_kind,
        budget_exhausted_count=1 if result.budget_exhausted else 0,
        reachable_hit=result.reachable,
        path_length=len(result.path),
        frontier_edge_count=result.frontier_edge_count,
        visited_function_count=result.visited_function_count,
        skipped_missing_endpoint_count=result.skipped_missing_endpoint_count,
    )

def _emit_event(
    self,
    event_name: str,
    *,
    status: str,
    operation: str,
    outcome: str,
    started: str,
    **payload: JSONValue,
) -> Tuple[int, Optional[str]]:
    if not self.log_enabled:
        return 0, None
    payload = dict(payload)
    payload.update(
        {
            "operation": operation,
            "outcome": outcome,
            "duration_ms": _elapsed_ms(started),
        }
    )
    try:
        open_log(self.target_repo).write_event(
            LogEvent(
                event_name=event_name,
                channel="storage",
                status=status,
                duration_ms=payload["duration_ms"],
                counts=_counts_from_payload(payload),
                error_code=payload.get("error_code") if status == "error" else None,
                payload=payload,
            )
        )
        return 0, None
    except LogError as exc:
        self._log_write_failures += 1
        self._latest_log_error_code = exc.code
        return 1, exc.code

def _emit_storage_error(self, error: StorageError, operation: str, started: str) -> None:
    failures, latest = self._emit_event(
        "storage.error",
        status="error",
        operation=operation,
        outcome="failed",
        started=started,
        error_code=error.code,
    )
    if failures:
        error.details["latest_log_error_code"] = latest
        error.details["log_write_failures"] = failures

def _ensure_cipher_path_safe(self) -> None:
    self._safe_path()

def _safe_path(self, *parts: str) -> Path:
    path = self.cipher_dir.joinpath(*parts)
    cipher_resolved = self.cipher_dir.resolve(strict=False)
    path_resolved = path.resolve(strict=False)
    if not _is_relative_to(path_resolved, cipher_resolved):
        raise StorageError("path_escape", "storage path escapes the fact store root", path=path)
    return path

def _acquire_lock(self, operation: str) -> Path:
    run_dir = self._safe_path("run")
    lock_dir = self._safe_path("run", "storage.lock")
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock_dir.mkdir()
    except FileExistsError as exc:
        raise StorageError("lock_busy", "storage lock is already held", path=lock_dir) from exc
    owner = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "created_at": _now(),
        "operation": operation,
    }
    (lock_dir / "owner.json").write_text(_canonical_json(owner) + "\n", encoding="utf-8")
    return lock_dir

def _release_lock(self, lock_dir: Path) -> None:
    if not lock_dir.exists():
        return
    try:
        for child in lock_dir.iterdir():
            child.unlink()
        lock_dir.rmdir()
    except OSError:
        pass

def _lock_state(self) -> str:
    lock_dir = self.cipher_dir / "run" / "storage.lock"
    if not lock_dir.exists():
        return "free"
    owner_path = lock_dir / "owner.json"
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "held"
    pid = owner.get("pid")
    if isinstance(pid, int) and not _pid_exists(pid):
        return "stale_likely"
    return "held"

def _snapshot_count(self, *, extra_snapshot_id: Optional[str] = None) -> int:
    snapshots = self.cipher_dir / "snapshots"
    names = set()
    if snapshots.exists():
        names.update(path.name for path in snapshots.iterdir() if path.is_dir())
    if extra_snapshot_id:
        names.add(extra_snapshot_id)
    return len(names)

def _all_snapshots_bytes(self, *, extra_current: int = 0, extra_snapshot_id: Optional[str] = None) -> int:
    snapshots = self.cipher_dir / "snapshots"
    total = 0
    if snapshots.exists():
        for path in snapshots.iterdir():
            if path.is_dir():
                total += _snapshot_bytes(path)
    if extra_snapshot_id and not (snapshots / extra_snapshot_id).exists():
        total += extra_current
    return total

__all__ = [name for name in globals() if not name.startswith("__")]

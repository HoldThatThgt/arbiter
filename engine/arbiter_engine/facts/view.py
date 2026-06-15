"""Writer-gated facts overlay view state, backed by the incremental coordinator.

The writer (player QUERY engine, ADR-0009) reconciles synchronously so adjudication is never
stale; readers report the coordinator's published overlay. The evidence view_state is "overlay"
only when a real fact overlay is active (sources are dirty vs the snapshot) — otherwise "base".
This replaced the Phase-1 placeholder that reported a census-digest overlay on every writer access.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from arbiter_engine import config
from arbiter_engine import errors
from arbiter_engine.facts import incremental
from arbiter_engine.facts import relocation
from arbiter_engine.facts.extractor.code._shim import ExtractorConfig
from arbiter_engine.shared import locks


@dataclass(frozen=True)
class AccessContext:
    role: str
    seat: str


@dataclass(frozen=True)
class FactView:
    view_state: str
    base_snapshot_id: Optional[str]
    overlay_id: Optional[str]
    stale_source_count: int = 0
    pending_task_count: int = 0

    def evidence(self) -> dict[str, Any]:
        return {
            "view_state": self.view_state,
            "base_snapshot_id": self.base_snapshot_id,
            "overlay_id": self.overlay_id,
            "stale_source_count": self.stale_source_count,
            "pending_task_count": self.pending_task_count,
        }


def overlay_state_path(repo: Path) -> Path:
    return incremental.overlay_pointer_path(Path(repo))


def access(repo: Path, context: AccessContext) -> FactView:
    if _is_writer(context):
        return reconcile(repo, context)
    return read_published(repo)


def refresh(repo: Path, context: AccessContext) -> FactView:
    if not _is_writer(context):
        raise errors.capability_revoked()
    return reconcile(repo, context)


def reconcile(repo: Path, context: AccessContext, *, timeout_s: float = 30.0) -> FactView:
    if not _is_writer(context):
        raise errors.capability_revoked()
    repo = Path(repo)
    # The OVERLAY flock is the single-writer gate (ADR-0009): only the player QUERY engine
    # reconciles, and the synchronous reconcile shares the flock with the background poll thread.
    with locks.acquire(repo, [locks.OVERLAY], timeout_s=timeout_s):
        coordinator = _build_coordinator(repo)
        status = coordinator.reconcile_current_sources()
        return _view_from_status(repo, status)


def read_published(repo: Path) -> FactView:
    repo = Path(repo)
    try:
        status = incremental.read_incremental_status(repo)
    except incremental.IncrementalError:
        return FactView(view_state="base", base_snapshot_id=_base_snapshot_id(repo), overlay_id=None)
    return _view_from_status(repo, status)


def _view_from_status(repo: Path, status: "incremental.IncrementalStatus") -> FactView:
    overlay_active = status.state == "overlay" and bool(status.overlay_id)
    return FactView(
        view_state="overlay" if overlay_active else "base",
        base_snapshot_id=status.base_snapshot_id or _base_snapshot_id(repo),
        overlay_id=status.overlay_id if overlay_active else None,
        stale_source_count=status.stale_source_count,
        pending_task_count=status.pending_task_count,
    )


def _build_coordinator(repo: Path) -> "incremental.IncrementalCoordinator":
    facts_config = _facts_config(repo)
    worker_count = facts_config.index_on_build.pool or (os.cpu_count() or 1)
    return incremental.IncrementalCoordinator(
        repo,
        facts_config.incremental,
        extractor_config=_reconcile_extractor_config(repo, worker_count),
        worker_count=worker_count,
        log_enabled=True,
    )


def _facts_config(repo: Path) -> "config.FactsConfig":
    try:
        return relocation.load_config(repo)
    except (OSError, config.ConfigError):
        return config.FactsConfig()


def _reconcile_extractor_config(repo: Path, worker_count: int) -> Optional[ExtractorConfig]:
    compile_db = relocation.persisted_compile_db_path(repo)
    if not compile_db.exists():
        # No build has published a compile-db: a dirty set can't be re-extracted (the coordinator
        # reports a typed error instead of guessing flags). Clean repos reconcile to "base" fine.
        return None
    return ExtractorConfig(compile_database_path=compile_db, extractor_worker_count=worker_count)


def _is_writer(context: AccessContext) -> bool:
    return context.role == "QUERY" and context.seat == "player"


def _base_snapshot_id(repo: Path) -> Optional[str]:
    current = relocation.facts_dir(repo) / "snapshots" / "current"
    try:
        if current.is_symlink():
            target = os.readlink(current)
            return Path(target).name or None
        if current.is_file():
            value = current.read_text(encoding="utf-8").strip()
            return value or None
        if current.is_dir():
            # The publisher writes a real "current" directory; the snapshot id
            # lives in its manifest, not in the directory name.
            return _manifest_snapshot_id(current / "manifest.json") or current.name
    except OSError:
        return None
    return None


def _manifest_snapshot_id(manifest_path: Path) -> Optional[str]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    snapshot_id = raw.get("snapshot_id")
    if isinstance(snapshot_id, str) and snapshot_id:
        return snapshot_id
    return None


__all__ = [
    "AccessContext",
    "FactView",
    "access",
    "overlay_state_path",
    "read_published",
    "reconcile",
    "refresh",
]

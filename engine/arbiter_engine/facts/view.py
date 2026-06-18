"""Writer-gated facts overlay view state, backed by the incremental coordinator.

The writer (player QUERY engine, ADR-0009) reconciles synchronously so adjudication is never
stale; readers report the coordinator's published overlay. The evidence view_state is "overlay"
only when a real fact overlay is active (sources are dirty vs the snapshot) — otherwise "base".
This replaced the Phase-1 placeholder that reported a census-digest overlay on every writer access.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from arbiter_engine import config
from arbiter_engine import errors
from arbiter_engine.facts import incremental
from arbiter_engine.facts import relocation
from arbiter_engine.facts.extractor.code._shim import ExtractorConfig, InitError, TOOLCHAIN_FAILURE_CODES
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
        try:
            status = coordinator.reconcile_current_sources()
        except InitError as exc:
            if exc.code not in TOOLCHAIN_FAILURE_CODES:
                raise
            # Mandatory-index hard stop, consistent with the build-tail publish: the indexer
            # toolchain is unusable, so the synchronous reconcile that gates fact predicates aborts
            # adjudication rather than silently returning a stale base view. (The background daemon
            # calls reconcile too but swallows per-tick exceptions, so it stays best-effort; only
            # this writer-synchronous path — arbiter/refresh and the writer search/detail access —
            # turns the failure into a typed error.)
            raise errors.indexer_unavailable(exc.code, exc.message)
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


def _reconcile_extractor_config(repo: Path, worker_count: int) -> ExtractorConfig:
    # The last build's compile-db (if any) gives the dirty re-extraction the build's exact flags;
    # without it the extractor still parses dirty sources with default flags + an auto-detected
    # toolchain. No capable toolchain at all -> the coordinator reports a typed error (base view).
    compile_db = relocation.persisted_compile_db_path(repo)
    return ExtractorConfig(
        compile_database_path=compile_db if compile_db.exists() else None,
        extractor_worker_count=worker_count,
        **relocation.extractor_toolchain_overrides(repo),
    )


def _is_writer(context: AccessContext) -> bool:
    return context.role == "QUERY" and context.seat == "player"


class BackgroundIndex:
    """Handle for the owner-required automatic background index (ADR-0018).

    A poll thread that keeps the incremental overlay warm between the referee's synchronous
    ``arbiter/refresh`` reconciles; adjudication stays never-stale on the synchronous path, so the
    daemon is a pure optimization. It shares the OVERLAY flock with the synchronous reconcile (each
    tick calls the same disk-idempotent ``reconcile``), so the two never race. Hosted by the player
    QUERY engine's serve loop: started at engine startup, stopped on stdin EOF (no orphan thread).
    """

    def __init__(self, thread: Optional[threading.Thread], stop_event: Optional[threading.Event]) -> None:
        self._thread = thread
        self._stop = stop_event

    @property
    def active(self) -> bool:
        return self._thread is not None

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def start_background_index(repo: Path, context: AccessContext) -> BackgroundIndex:
    if not _is_writer(context):
        return BackgroundIndex(None, None)
    repo = Path(repo)
    incremental_config = _facts_config(repo).incremental
    if not incremental_config.enabled:
        return BackgroundIndex(None, None)
    stop = threading.Event()
    thread = threading.Thread(
        target=_background_loop,
        args=(repo, context, max(0.05, incremental_config.poll_interval_ms / 1000.0), stop),
        name="arbiter-facts-bg-index",
        daemon=True,
    )
    thread.start()
    return BackgroundIndex(thread, stop)


def _background_loop(repo: Path, context: AccessContext, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            reconcile(repo, context, timeout_s=2.0)
        except Exception:  # noqa: BLE001 - a busy OVERLAY lock or transient extract error just skips this tick.
            pass


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
    "BackgroundIndex",
    "FactView",
    "access",
    "overlay_state_path",
    "read_published",
    "reconcile",
    "refresh",
    "start_background_index",
]

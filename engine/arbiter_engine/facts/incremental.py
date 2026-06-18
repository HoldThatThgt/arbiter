"""Temporary online incremental overlays for arbiter (M4 Phase 2, ADR-0018).

Near-verbatim port of cipher-2's ``cipher2.incremental`` coordinator, adapted to arbiter:
- config knobs come from ``IncrementalConfig`` (the live ``facts.incremental`` section);
  ``worker_count`` is the unified ``facts.index_on_build.pool`` (owner decision 2).
- the real jsonl audit log lives at ``arbiter_engine.facts.log`` (the store/extractor
  run log-disabled; the coordinator's audit trail is part of its contract).
- overlay ids are content-addressed (``overlay-<sha16>``) instead of cipher-2's random
  UUID, matching arbiter's ``overlay:<digest>`` convention (proposal §6.2.2).
- paths are relocated under ``.arbiter/facts/run/incremental/`` (cipher-2 used ``.cipher``).

The coordinator is the facts single-writer (player QUERY engine, ADR-0009): a synchronous
``reconcile_current_sources`` keeps adjudication never-stale, and an optional background
poll thread keeps the overlay warm between refreshes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol

from arbiter_engine.config import IncrementalConfig
from arbiter_engine.facts import relocation
from arbiter_engine.facts.extractor.code import CodeFactExtractor
from arbiter_engine.facts.extractor.code._shim import (
    ExtractorConfig,
    IncrementalBuildResult,
    InitError,
    TOOLCHAIN_FAILURE_CODES,
)
from arbiter_engine.facts.log import JsonlLog, LogError, LogEvent, open_log
from arbiter_engine.facts.store import (
    FactRecord,
    FactRelative,
    FactView,
    SourceInventoryEntry,
    StorageError,
    TemporaryOverlay,
    open_fact_store,
)
from arbiter_engine.facts.store._common import JSONValue

RUN_DIR = ("run", "incremental")
STATE_FILENAME = "state.json"


class IncrementalError(Exception):
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


@dataclass(frozen=True)
class DirtySource:
    source_id: str
    rel_path: str
    reason: str
    previous_sha256: Optional[str]
    current_sha256: Optional[str]
    fanout_count: int = 0


@dataclass(frozen=True)
class IncrementalStatus:
    state: str
    base_snapshot_id: Optional[str]
    overlay_id: Optional[str] = None
    dirty_source_count: int = 0
    pending_task_count: int = 0
    stale_source_count: int = 0
    failed_task_count: int = 0
    overlay_fact_count: int = 0
    overlay_relative_count: int = 0
    last_publish_latency_ms: Optional[float] = None
    latest_error_code: Optional[str] = None

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "state": self.state,
            "base_snapshot_id": self.base_snapshot_id,
            "active_overlay_id": self.overlay_id,
            "dirty_source_count": self.dirty_source_count,
            "pending_task_count": self.pending_task_count,
            "stale_source_count": self.stale_source_count,
            "failed_task_count": self.failed_task_count,
            "overlay_fact_count": self.overlay_fact_count,
            "overlay_relative_count": self.overlay_relative_count,
            "last_publish_latency_ms": self.last_publish_latency_ms,
            "latest_error_code": self.latest_error_code,
        }


class DirtyExtractor(Protocol):
    def extract_dirty_sources(self, dirty_sources: Iterable[DirtySource], profile: str) -> IncrementalBuildResult:
        ...


class IncrementalCoordinator:
    def __init__(
        self,
        target_repo: Path,
        config: IncrementalConfig,
        *,
        extractor: Optional[DirtyExtractor] = None,
        extractor_config: Optional[ExtractorConfig] = None,
        worker_count: int = 1,
        log_enabled: bool = True,
        profile: str = "default",
    ) -> None:
        self.target_repo = Path(target_repo)
        self.config = config
        self.worker_count = max(1, int(worker_count))
        self.profile = profile
        self.log_enabled = log_enabled
        # The coordinator's audit trail lives under run/log/ (beside the rest of run/ state),
        # isolated from the extractor's facts/log/ stream; channel "incremental".
        self.log: JsonlLog = open_log(self.target_repo, base_parts=("facts", "run", "log"))
        self.generation = 0
        self._extractor = extractor
        self._extractor_config = extractor_config
        self._active_overlay: Optional[TemporaryOverlay] = None
        self._notify_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._observed_sha256_by_path: Dict[str, str] = {}
        self.active_view = open_fact_store(self.target_repo, mode="r", log_enabled=False).open_view(None)
        self._status = IncrementalStatus("disabled" if not config.enabled else "ready", self.active_view.base_snapshot_id)
        self._write_state(self._status)

    def start(self) -> IncrementalStatus:
        if not self.config.enabled:
            self._write_state(self._status)
            return self._status
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return self._status
        self._stop_event.clear()
        self._prime_observed_sources()
        self._emit(
            "incremental.poll_started",
            "ok",
            counts={"worker_count": self.worker_count},
            payload={
                "base_snapshot_id": self.active_view.base_snapshot_id,
                "poll_interval_ms": self.config.poll_interval_ms,
                "debounce_ms": self.config.debounce_ms,
            },
        )
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="arbiter-incremental-poll",
            daemon=True,
        )
        self._poll_thread.start()
        return self._status

    def stop(self) -> IncrementalStatus:
        self._stop_event.set()
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=max(1.0, self.config.poll_interval_ms / 1000.0 + 0.5))
        self._poll_thread = None
        if self._active_overlay is not None:
            self._drop_overlay("stop")
        self._status = IncrementalStatus("disabled", self.active_view.base_snapshot_id)
        self._write_state(self._status)
        return self._status

    def current_view(self) -> FactView:
        return self.active_view

    def status(self) -> IncrementalStatus:
        return self._status

    def reconcile_current_sources(self) -> IncrementalStatus:
        with self._notify_lock:
            return self._reconcile_current_sources()

    def notify_file_changed(self, path: Path) -> IncrementalStatus:
        with self._notify_lock:
            return self._notify_file_changed(path)

    def _reconcile_current_sources(self) -> IncrementalStatus:
        if not self.config.enabled:
            return self._status
        started = time.perf_counter()
        try:
            store = open_fact_store(self.target_repo, mode="r", log_enabled=False)
            base_snapshot_id = store.stats().snapshot_id
            inventory = list(store.iter_source_inventory())
        except StorageError as exc:
            return self._fail(exc.code, self.active_view.base_snapshot_id, started)
        if not inventory:
            self._status = IncrementalStatus("ready", base_snapshot_id)
            self._write_state(self._status)
            return self._status

        dirty_by_source: Dict[str, DirtySource] = {}
        changed_entries: List[SourceInventoryEntry] = []
        for entry in inventory:
            if entry.source_kind not in {"c_source", "header"}:
                continue
            source_path = self.target_repo / entry.rel_path
            try:
                stat = source_path.stat()
            except OSError:
                continue
            if not _source_may_have_changed(entry, stat):
                continue
            try:
                current_sha256 = _file_sha256(source_path)
            except OSError:
                continue
            self._observed_sha256_by_path[entry.rel_path] = current_sha256
            if current_sha256 == entry.sha256:
                continue
            changed_entries.append(entry)
            for dirty in self._plan_dirty_sources(entry, inventory, current_sha256):
                existing = dirty_by_source.get(dirty.source_id)
                if existing is None or _dirty_source_priority(dirty.reason) < _dirty_source_priority(existing.reason):
                    dirty_by_source[dirty.source_id] = dirty

        dirty_sources = sorted(dirty_by_source.values(), key=lambda item: (item.rel_path, item.source_id))
        if not dirty_sources:
            self._status = IncrementalStatus("ready", base_snapshot_id)
            self._write_state(self._status)
            return self._status
        if len(dirty_sources) > self.config.max_dirty_files:
            return self._fail("dirty_set_too_large", base_snapshot_id, started, dirty_count=len(dirty_sources))

        self._emit(
            "incremental.file_changed",
            "ok",
            counts={"changed_file_count": len(changed_entries)},
            payload={
                "source_id": changed_entries[0].source_id,
                "rel_path": changed_entries[0].rel_path,
            },
        )
        self.generation += 1
        self._status = IncrementalStatus(
            "pending",
            base_snapshot_id,
            dirty_source_count=len(dirty_sources),
            pending_task_count=1,
            stale_source_count=len(dirty_sources),
        )
        self._write_state(self._status)
        self._emit(
            "incremental.dirty_planned",
            "ok",
            counts={"dirty_source_count": len(dirty_sources), "fanout_count": sum(item.fanout_count for item in dirty_sources)},
            payload={"reason": dirty_sources[0].reason, "base_snapshot_id": base_snapshot_id},
        )
        self._emit(
            "incremental.extract_started",
            "ok",
            counts={"dirty_source_count": len(dirty_sources)},
            payload={"task_id": f"task-{self.generation}", "generation": self.generation},
        )
        try:
            result = self._extract_dirty(dirty_sources)
            return self._publish_overlay(store, base_snapshot_id, dirty_sources, result, started)
        except IncrementalError as exc:
            return self._fail(exc.code, base_snapshot_id, started, dirty_count=len(dirty_sources))
        except InitError as exc:
            if exc.code in TOOLCHAIN_FAILURE_CODES:
                # Mandatory-index hard stop: the indexer toolchain is unusable, so the synchronous
                # reconcile must abort (view.reconcile surfaces indexer_unavailable) rather than
                # leave adjudication reading a stale view. The background daemon swallows it per-tick.
                # Non-toolchain InitErrors stay graceful, like every other extract failure.
                raise
            return self._fail("clang_ast_failed", base_snapshot_id, started, dirty_count=len(dirty_sources))
        except Exception:
            return self._fail("clang_ast_failed", base_snapshot_id, started, dirty_count=len(dirty_sources))

    def _notify_file_changed(self, path: Path) -> IncrementalStatus:
        if not self.config.enabled:
            return self._status
        started = time.perf_counter()
        rel_path = self._normalize_changed_path(path)
        store = open_fact_store(self.target_repo, mode="r", log_enabled=False)
        base_snapshot_id = store.stats().snapshot_id
        inventory = list(store.iter_source_inventory())
        if not inventory:
            return self._fail("inventory_missing", base_snapshot_id, started)
        by_path = {entry.rel_path: entry for entry in inventory}
        entry = by_path.get(rel_path)
        if entry is None:
            return self._fail("compile_command_missing", base_snapshot_id, started)
        try:
            current_sha256 = _file_sha256(self.target_repo / rel_path)
        except OSError:
            return self._fail("source_unreadable", base_snapshot_id, started, dirty_count=1)
        self._observed_sha256_by_path[rel_path] = current_sha256
        self._emit(
            "incremental.file_changed",
            "ok",
            counts={"changed_file_count": 1},
            payload={"source_id": entry.source_id, "rel_path": rel_path},
        )
        if current_sha256 == entry.sha256:
            if self._active_overlay is not None and entry.source_id in self._active_overlay.source_tombstones:
                self._drop_overlay("reverted_to_base")
            self._status = IncrementalStatus("ready", base_snapshot_id)
            self._write_state(self._status)
            return self._status
        dirty_sources = self._plan_dirty_sources(entry, inventory, current_sha256)
        if len(dirty_sources) > self.config.max_dirty_files:
            return self._fail("dirty_set_too_large", base_snapshot_id, started, dirty_count=len(dirty_sources))
        self.generation += 1
        self._status = IncrementalStatus(
            "pending",
            base_snapshot_id,
            dirty_source_count=len(dirty_sources),
            pending_task_count=1,
            stale_source_count=len(dirty_sources),
        )
        self._write_state(self._status)
        self._emit(
            "incremental.dirty_planned",
            "ok",
            counts={"dirty_source_count": len(dirty_sources), "fanout_count": sum(item.fanout_count for item in dirty_sources)},
            payload={"reason": dirty_sources[0].reason, "base_snapshot_id": base_snapshot_id},
        )
        self._emit(
            "incremental.extract_started",
            "ok",
            counts={"dirty_source_count": len(dirty_sources)},
            payload={"task_id": f"task-{self.generation}", "generation": self.generation},
        )
        try:
            result = self._extract_dirty(dirty_sources)
            return self._publish_overlay(store, base_snapshot_id, dirty_sources, result, started)
        except IncrementalError as exc:
            return self._fail(exc.code, base_snapshot_id, started, dirty_count=len(dirty_sources))
        except InitError as exc:
            if exc.code in TOOLCHAIN_FAILURE_CODES:
                # Mandatory-index hard stop: the indexer toolchain is unusable, so the synchronous
                # reconcile must abort (view.reconcile surfaces indexer_unavailable) rather than
                # leave adjudication reading a stale view. The background daemon swallows it per-tick.
                # Non-toolchain InitErrors stay graceful, like every other extract failure.
                raise
            return self._fail("clang_ast_failed", base_snapshot_id, started, dirty_count=len(dirty_sources))
        except Exception:
            return self._fail("clang_ast_failed", base_snapshot_id, started, dirty_count=len(dirty_sources))

    def _normalize_changed_path(self, path: Path) -> str:
        target = self.target_repo.resolve(strict=False)
        resolved = Path(path).resolve(strict=False)
        if not _is_relative_to(resolved, target):
            raise IncrementalError("path_escape", "changed path escapes target repository")
        rel = resolved.relative_to(target).as_posix()
        if rel.startswith(".arbiter/") or rel == ".arbiter":
            raise IncrementalError("path_escape", "changed path cannot be inside .arbiter")
        return rel

    def _plan_dirty_sources(
        self,
        entry: SourceInventoryEntry,
        inventory: List[SourceInventoryEntry],
        current_sha256: str,
    ) -> List[DirtySource]:
        if entry.source_kind != "header":
            return [
                DirtySource(
                    source_id=entry.source_id,
                    rel_path=entry.rel_path,
                    reason="content_changed",
                    previous_sha256=entry.sha256,
                    current_sha256=current_sha256,
                    fanout_count=0,
                )
            ]
        by_id = {item.source_id: item for item in inventory}
        affected = [by_id[source_id] for source_id in entry.included_by if source_id in by_id]
        if not affected:
            affected = [entry]
        return [
            DirtySource(
                source_id=item.source_id,
                rel_path=item.rel_path,
                reason="included_header_changed" if item.source_id != entry.source_id else "content_changed",
                previous_sha256=item.sha256,
                current_sha256=current_sha256 if item.source_id == entry.source_id else item.sha256,
                fanout_count=len(affected),
            )
            for item in affected
        ]

    def _extract_dirty(self, dirty_sources: List[DirtySource]) -> IncrementalBuildResult:
        extractor = self._extractor
        if extractor is None:
            if self._extractor_config is None:
                raise IncrementalError("toolchain_unavailable", "no extractor configured for dirty re-extraction")
            extractor = CodeFactExtractor(self.target_repo, self._extractor_config)
        result = extractor.extract_dirty_sources(dirty_sources, self.profile)
        if isinstance(result, IncrementalBuildResult):
            return result
        facts = [fact.to_fact_record() if hasattr(fact, "to_fact_record") else fact for fact in result.facts]
        return IncrementalBuildResult(facts=facts, relatives=list(result.relatives), source_inventory=list(getattr(result, "source_inventory", [])))

    def _publish_overlay(
        self,
        store,
        base_snapshot_id: Optional[str],
        dirty_sources: List[DirtySource],
        result: IncrementalBuildResult,
        started: float,
    ) -> IncrementalStatus:
        publish_started = time.perf_counter()
        source_ids = {item.source_id for item in dirty_sources}
        self._validate_overlay_relatives(store, source_ids, result)
        fact_tombstones = [
            _overlay_patch_line(
                source_id=source_id,
                action="delete_fact",
                object_id=None,
                relative_id=None,
                payload={"source_id": source_id},
            )
            for source_id in sorted(source_ids)
        ]
        source_rel_paths = {item.rel_path for item in dirty_sources}
        base_relative_tombstones = [
            relative
            for relative in store.iter_relatives()
            if _relative_belongs_to_dirty_source(relative, source_ids, source_rel_paths)
        ]
        relative_tombstones = [
            _overlay_patch_line(
                source_id=_source_id_from_relative(relative) or dirty_sources[0].source_id,
                action="delete_relative",
                object_id=None,
                relative_id=relative.relative_id,
                payload={
                    "source_id": _source_id_from_relative(relative) or "",
                    "evidence_source": relative.evidence_source,
                },
            )
            for relative in base_relative_tombstones
        ]
        fact_upserts = [
            _overlay_patch_line(
                source_id=_source_id_from_fact(fact) or dirty_sources[0].source_id,
                action="upsert_fact",
                object_id=fact.object_id,
                relative_id=None,
                payload=fact.to_json(),
            )
            for fact in result.facts
        ]
        relative_upserts = [
            _overlay_patch_line(
                source_id=_source_id_from_relative(relative) or dirty_sources[0].source_id,
                action="upsert_relative",
                object_id=None,
                relative_id=relative.relative_id,
                payload=relative.to_json(),
            )
            for relative in result.relatives
        ]
        overlay_id = _content_overlay_id(
            base_snapshot_id, fact_upserts, fact_tombstones, relative_upserts, relative_tombstones
        )
        overlay_dir = self._run_path("overlays", overlay_id)
        overlay_dir.mkdir(parents=True, exist_ok=True)
        _write_jsonl(overlay_dir / "facts.upsert.jsonl", fact_upserts)
        _write_jsonl(overlay_dir / "facts.tombstone.jsonl", fact_tombstones)
        _write_jsonl(overlay_dir / "relatives.upsert.jsonl", relative_upserts)
        _write_jsonl(overlay_dir / "relatives.tombstone.jsonl", relative_tombstones)
        created_at = _utc_now()
        (overlay_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "overlay_id": overlay_id,
                    "base_snapshot_id": base_snapshot_id,
                    "generation": self.generation,
                    "status": "published",
                    "created_at": created_at,
                    "published_at": _utc_now(),
                    "dirty_source_count": len(dirty_sources),
                    "fact_upsert_count": len(result.facts),
                    "fact_tombstone_count": len(fact_tombstones),
                    "relative_upsert_count": len(result.relatives),
                    "relative_tombstone_count": len(relative_tombstones),
                    "error_code": None,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        (overlay_dir / "stats.json").write_text(
            json.dumps(
                {
                    "overlay_id": overlay_id,
                    "base_snapshot_id": base_snapshot_id,
                    "dirty_source_count": len(dirty_sources),
                    "fact_upsert_count": len(result.facts),
                    "fact_tombstone_count": len(fact_tombstones),
                    "relative_upsert_count": len(result.relatives),
                    "relative_tombstone_count": len(relative_tombstones),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        overlay = TemporaryOverlay(
            overlay_id=overlay_id,
            view_state="overlay",
            fact_upserts=list(result.facts),
            relative_upserts=list(result.relatives),
            relative_tombstones={relative.relative_id for relative in base_relative_tombstones},
            source_tombstones=source_ids,
        )
        self._active_overlay = overlay
        self.active_view = store.open_view(overlay)
        self._write_overlay_pointer(base_snapshot_id, overlay_id)
        latency = max(0.0, (time.perf_counter() - publish_started) * 1000)
        self._emit(
            "incremental.overlay_built",
            "ok",
            counts={
                "fact_upsert_count": len(result.facts),
                "relative_upsert_count": len(result.relatives),
                "tombstone_count": len(source_ids) + len(relative_tombstones),
            },
            payload={"overlay_id": overlay_id},
        )
        self._emit(
            "incremental.overlay_published",
            "ok",
            counts={"overlay_fact_count": len(result.facts), "overlay_relative_count": len(result.relatives)},
            payload={
                "base_snapshot_id": base_snapshot_id,
                "overlay_id": overlay_id,
                "view_id": self.active_view.view_id,
                "view_state": "overlay",
                "publish_latency_ms": latency,
            },
        )
        self._status = IncrementalStatus(
            "overlay",
            base_snapshot_id,
            overlay_id=overlay_id,
            dirty_source_count=len(dirty_sources),
            overlay_fact_count=len(result.facts),
            overlay_relative_count=len(result.relatives),
            last_publish_latency_ms=latency,
        )
        self._write_state(self._status)
        return self._status

    def _validate_overlay_relatives(
        self,
        store,
        source_ids: set[str],
        result: IncrementalBuildResult,
    ) -> None:
        visible_fact_ids = {
            fact.object_id
            for fact in store.iter_facts()
            if _source_id_from_fact(fact) not in source_ids
        }
        visible_fact_ids.update(fact.object_id for fact in result.facts)
        for relative in result.relatives:
            if relative.from_fact_id not in visible_fact_ids or relative.to_fact_id not in visible_fact_ids:
                raise IncrementalError(
                    "overlay_endpoint_orphan",
                    "overlay relative endpoint is not visible",
                    details={"relative_id": relative.relative_id},
                )

    def _drop_overlay(self, reason: str) -> None:
        self._emit(
            "incremental.overlay_dropped",
            "ok",
            counts={"dropped_overlay_count": 1},
            payload={"reason": reason},
        )
        overlays = self._run_path("overlays")
        if overlays.exists():
            shutil.rmtree(overlays, ignore_errors=True)
        self._active_overlay = None
        self.active_view = open_fact_store(self.target_repo, mode="r", log_enabled=False).open_view(None)
        self._clear_overlay_pointer()

    def _fail(
        self,
        code: str,
        base_snapshot_id: Optional[str],
        started: float,
        *,
        dirty_count: int = 0,
    ) -> IncrementalStatus:
        self._emit(
            "incremental.extract_failed",
            "error",
            counts={"dirty_source_count": dirty_count},
            payload={"error_code": code, "task_id": f"task-{self.generation}"},
            error_code=code,
        )
        self._status = IncrementalStatus(
            "error",
            base_snapshot_id,
            dirty_source_count=dirty_count,
            stale_source_count=dirty_count,
            failed_task_count=1,
            latest_error_code=code,
        )
        self._write_state(self._status)
        return self._status

    def _run_path(self, *parts: str) -> Path:
        for part in parts:
            if "/" in part or "\\" in part or part in {"", ".", ".."}:
                raise IncrementalError("path_escape", "overlay path component is unsafe")
        return relocation.facts_dir(self.target_repo).joinpath(*RUN_DIR, *parts)

    def _write_state(self, status: IncrementalStatus) -> None:
        path = self._run_path(STATE_FILENAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status.to_json(), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    def _write_overlay_pointer(self, base_snapshot_id: Optional[str], overlay_id: str) -> None:
        path = overlay_pointer_path(self.target_repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"base_snapshot_id": base_snapshot_id, "overlay_id": overlay_id, "view_state": "overlay"},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    def _clear_overlay_pointer(self) -> None:
        try:
            overlay_pointer_path(self.target_repo).unlink()
        except OSError:
            pass

    def _emit(
        self,
        event_name: str,
        status: str,
        *,
        counts: Optional[Dict[str, int]] = None,
        payload: Optional[Dict[str, JSONValue]] = None,
        error_code: Optional[str] = None,
    ) -> None:
        if not self.log_enabled:
            return
        try:
            self.log.write_event(
                LogEvent(
                    event_name=event_name,
                    channel="incremental",
                    status=status,
                    counts=counts or {},
                    payload=payload or {},
                    error_code=error_code,
                )
            )
        except LogError:
            pass

    def _prime_observed_sources(self) -> None:
        try:
            inventory = list(open_fact_store(self.target_repo, mode="r", log_enabled=False).iter_source_inventory())
        except StorageError:
            return
        for entry in inventory:
            self._observed_sha256_by_path.setdefault(entry.rel_path, entry.sha256)

    def _poll_loop(self) -> None:
        interval = self.config.poll_interval_ms / 1000.0
        while not self._stop_event.wait(interval):
            try:
                self._gc_aged_overlay()
                self._scan_once()
            except Exception:  # noqa: BLE001 - the background poll is best-effort and must outlive
                # any single tick, including the mandatory-index hard stop a broken toolchain now
                # raises from the reconcile path. A bad tick is skipped, never fatal; the synchronous
                # reconcile (view.reconcile) is what surfaces the failure as indexer_unavailable.
                continue

    def _gc_aged_overlay(self) -> None:
        """Reap a published overlay once its ``created_at`` ages past the TTL (ADR-0018).

        ``overlay_ttl_seconds == 0`` means "never GC" (user-guide §9). The age is read back
        from the overlay manifest the publish path already stamps, against ``time.time()``;
        teardown reuses ``_drop_overlay`` to keep the single-writer drop semantics ('stop' /
        'reverted_to_base') and the overlay lock discipline (ADR-0009) intact.
        """
        ttl = self.config.overlay_ttl_seconds
        if ttl <= 0:
            return
        with self._notify_lock:
            overlay = self._active_overlay
            if overlay is None:
                return
            manifest = self._run_path("overlays", overlay.overlay_id, "manifest.json")
            created_at = _read_overlay_created_at(manifest)
            if created_at is None or time.time() - created_at < ttl:
                return
            self._drop_overlay("overlay_ttl_expired")
            self._status = IncrementalStatus("ready", self.active_view.base_snapshot_id)
            self._write_state(self._status)

    def _scan_once(self) -> None:
        if not self.config.enabled:
            return
        started = time.perf_counter()
        try:
            store = open_fact_store(self.target_repo, mode="r", log_enabled=False)
            base_snapshot_id = store.stats().snapshot_id
            inventory = list(store.iter_source_inventory())
        except StorageError as exc:
            self._fail(exc.code, self.active_view.base_snapshot_id, started)
            return
        if not inventory:
            self._fail("inventory_missing", base_snapshot_id, started)
            return
        for entry in inventory:
            source_path = self.target_repo / entry.rel_path
            try:
                current_sha256 = _file_sha256(source_path)
            except OSError:
                self._fail("source_unreadable", base_snapshot_id, started, dirty_count=1)
                continue
            observed_sha256 = self._observed_sha256_by_path.setdefault(entry.rel_path, entry.sha256)
            if current_sha256 == observed_sha256:
                continue
            if self._stop_event.wait(self.config.debounce_ms / 1000.0):
                return
            status = self.notify_file_changed(source_path)
            if status.state in {"overlay", "ready", "error"}:
                self._observed_sha256_by_path[entry.rel_path] = current_sha256
            return


def overlay_pointer_path(target_repo: Path) -> Path:
    return relocation.facts_dir(Path(target_repo)) / "overlay" / "current.json"


def load_active_overlay(target_repo: Path) -> Optional[TemporaryOverlay]:
    """Reconstruct the published overlay from disk so any reader can merge it.

    The coordinator persists the patchset under ``run/incremental/overlays/<id>/`` and a
    pointer at ``overlay/current.json``. Readers (rpc search/detail) load it here and pass
    it to ``store.open_view(overlay)`` — the overlay state survives across stateless calls
    and across processes, without reaching into the coordinator's in-memory view.
    """
    pointer = overlay_pointer_path(target_repo)
    try:
        raw = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    overlay_id = raw.get("overlay_id")
    if not isinstance(overlay_id, str) or not overlay_id:
        return None
    overlay_dir = relocation.facts_dir(Path(target_repo)).joinpath(*RUN_DIR, "overlays", overlay_id)
    if not overlay_dir.is_dir():
        return None
    fact_upserts = [FactRecord.from_json(row["payload"]) for row in _read_jsonl(overlay_dir / "facts.upsert.jsonl")]
    relative_upserts = [FactRelative.from_json(row["payload"]) for row in _read_jsonl(overlay_dir / "relatives.upsert.jsonl")]
    fact_tombstones = {row["payload"]["source_id"] for row in _read_jsonl(overlay_dir / "facts.tombstone.jsonl")}
    relative_tombstones = {
        row["relative_id"]
        for row in _read_jsonl(overlay_dir / "relatives.tombstone.jsonl")
        if row.get("relative_id")
    }
    try:
        return TemporaryOverlay(
            overlay_id=overlay_id,
            view_state="overlay",
            fact_upserts=fact_upserts,
            relative_upserts=relative_upserts,
            relative_tombstones=relative_tombstones,
            source_tombstones=set(fact_tombstones),
        )
    except StorageError:
        return None


def read_incremental_status(target_repo: Path) -> IncrementalStatus:
    path = relocation.facts_dir(Path(target_repo)).joinpath(*RUN_DIR, STATE_FILENAME)
    if not path.exists():
        return IncrementalStatus("disabled", None)
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IncrementalError("incremental_unreadable", "incremental state is unreadable") from exc
    return IncrementalStatus(
        state=row.get("state", "disabled"),
        base_snapshot_id=row.get("base_snapshot_id"),
        overlay_id=row.get("active_overlay_id"),
        dirty_source_count=int(row.get("dirty_source_count", 0) or 0),
        pending_task_count=int(row.get("pending_task_count", 0) or 0),
        stale_source_count=int(row.get("stale_source_count", 0) or 0),
        failed_task_count=int(row.get("failed_task_count", 0) or 0),
        overlay_fact_count=int(row.get("overlay_fact_count", 0) or 0),
        overlay_relative_count=int(row.get("overlay_relative_count", 0) or 0),
        last_publish_latency_ms=row.get("last_publish_latency_ms"),
        latest_error_code=row.get("latest_error_code"),
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_jsonl(path: Path, rows: Iterable[Dict[str, JSONValue]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> List[Dict[str, JSONValue]]:
    rows: List[Dict[str, JSONValue]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    except (OSError, json.JSONDecodeError):
        return rows
    return rows


def _overlay_patch_line(
    *,
    source_id: str,
    action: str,
    object_id: Optional[str],
    relative_id: Optional[str],
    payload: Dict[str, JSONValue],
) -> Dict[str, JSONValue]:
    return {
        "source_id": source_id,
        "action": action,
        "object_id": object_id,
        "relative_id": relative_id,
        "payload": payload,
        "payload_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }


def _content_overlay_id(
    base_snapshot_id: Optional[str],
    fact_upserts: List[Dict[str, JSONValue]],
    fact_tombstones: List[Dict[str, JSONValue]],
    relative_upserts: List[Dict[str, JSONValue]],
    relative_tombstones: List[Dict[str, JSONValue]],
) -> str:
    digest = hashlib.sha256()
    digest.update((base_snapshot_id or "").encode("utf-8"))
    for group in (fact_upserts, fact_tombstones, relative_upserts, relative_tombstones):
        digest.update(b"\0")
        for line in sorted(item["payload_sha256"] for item in group):
            digest.update(line.encode("ascii"))
            digest.update(b"\0")
    return "overlay-" + digest.hexdigest()[:16]


def _source_id_from_fact(fact: FactRecord) -> Optional[str]:
    value = fact.payload.get("source_id")
    return value if isinstance(value, str) and value else None


def _source_id_from_relative(relative: FactRelative) -> Optional[str]:
    value = relative.payload.get("source_id")
    return value if isinstance(value, str) and value else None


def _source_may_have_changed(entry: SourceInventoryEntry, stat_result) -> bool:
    return stat_result.st_mtime_ns > entry.mtime_ns or stat_result.st_size != entry.size_bytes


def _relative_belongs_to_dirty_source(
    relative: FactRelative,
    source_ids: set[str],
    source_rel_paths: set[str],
) -> bool:
    source_id = _source_id_from_relative(relative)
    if source_id in source_ids:
        return True
    evidence_source = _source_ref_file(relative.evidence_source)
    return evidence_source in source_rel_paths


def _source_ref_file(value: str) -> str:
    path, separator, line = value.rpartition(":")
    if separator and path and line.isdigit() and int(line) > 0:
        return path
    return value


def _dirty_source_priority(reason: str) -> int:
    return {
        "content_changed": 0,
        "missing": 1,
        "compile_command_changed": 2,
        "toolchain_changed": 3,
        "included_header_changed": 4,
    }.get(reason, 5)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_overlay_created_at(manifest_path: Path) -> Optional[float]:
    """Return the overlay's ``created_at`` as a POSIX timestamp, or None if unreadable.

    Mirrors the inverse of ``_utc_now``: the manifest stamps an ISO-8601 UTC string with a
    trailing ``Z`` (``datetime.fromisoformat`` only accepts ``Z`` on Python >= 3.11, so the
    suffix is normalized to ``+00:00`` first). A timezone-naive value is treated as UTC.
    """
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    created_at = raw.get("created_at") if isinstance(raw, dict) else None
    if not isinstance(created_at, str) or not created_at:
        return None
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


__all__ = [
    "DirtySource",
    "IncrementalBuildResult",
    "IncrementalConfig",
    "IncrementalCoordinator",
    "IncrementalError",
    "IncrementalStatus",
    "load_active_overlay",
    "overlay_pointer_path",
    "read_incremental_status",
]

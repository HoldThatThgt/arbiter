"""Temporary online incremental overlays for cipher-2."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol

from cipher2.common import JSONValue
from cipher2.config import CipherConfig, safe_cipher_path
from cipher2.initializer.extractor.code.compile_db import (
    _MalformedCompileDatabaseError,
    _compile_command_entry_from_mapping,
)
from cipher2.initializer.extractor.code import CodeFactExtractor
from cipher2.storage import (
    FactRecord,
    FactRelative,
    FactView,
    SourceInventoryEntry,
    StorageError,
    TemporaryOverlay,
    open_fact_store,
)
from cipher2.tools.log import LogError, LogEvent, JsonlLog, open_log


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
class IncrementalBuildResult:
    facts: List[FactRecord] = field(default_factory=list)
    relatives: List[FactRelative] = field(default_factory=list)
    source_inventory: List[SourceInventoryEntry] = field(default_factory=list)


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


@dataclass
class OverlayRuntimeGuard:
    overlay_id: str
    base_snapshot_id: Optional[str]
    storage_schema_fingerprint: str
    source_compile_command_hashes: Dict[str, Optional[str]]
    source_toolchain_hashes: Dict[str, str]
    published_at_monotonic: float
    last_access_monotonic: float


class IncrementalCoordinator:
    def __init__(
        self,
        target_repo: Path,
        config: CipherConfig,
        *,
        extractor: Optional[DirtyExtractor] = None,
        log_enabled: bool = True,
        profile: str = "default",
    ) -> None:
        self.target_repo = Path(target_repo)
        self.config = config
        self.profile = profile
        self.log_enabled = log_enabled
        self.log: JsonlLog = open_log(self.target_repo)
        self.generation = 0
        self._extractor = extractor
        self._active_overlay: Optional[TemporaryOverlay] = None
        self._overlay_guard: Optional[OverlayRuntimeGuard] = None
        self._toolchain_hash_cache_ready = False
        self._toolchain_hash_cache: Optional[str] = None
        self._notify_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._observed_sha256_by_path: Dict[str, str] = {}
        self.active_view = open_fact_store(self.target_repo, mode="r", log_enabled=False).open_view(None)
        self._status = IncrementalStatus("disabled" if not config.incremental_temporary_enabled else "ready", self.active_view.base_snapshot_id)
        self._write_state(self._status)

    def start(self) -> IncrementalStatus:
        if not self.config.incremental_temporary_enabled:
            self._write_state(self._status)
            return self._status
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return self._status
        self._stop_event.clear()
        self._prime_observed_sources()
        self._emit(
            "incremental.poll_started",
            "ok",
            counts={
                "worker_count": self.config.incremental_worker_count,
                "configured_worker_count": self.config.incremental_worker_count,
                "active_worker_count": 1,
            },
            payload={
                "base_snapshot_id": self.active_view.base_snapshot_id,
                "poll_interval_ms": self.config.incremental_poll_interval_ms,
                "debounce_ms": self.config.incremental_debounce_ms,
            },
        )
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="cipher2-incremental-poll",
            daemon=True,
        )
        self._poll_thread.start()
        return self._status

    def stop(self) -> IncrementalStatus:
        self._stop_event.set()
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=max(1.0, self.config.incremental_poll_interval_ms / 1000.0 + 0.5))
        self._poll_thread = None
        if self._active_overlay is not None:
            self._drop_overlay("stop")
        self._status = IncrementalStatus("disabled", self.active_view.base_snapshot_id)
        self._write_state(self._status)
        return self._status

    def current_view(self) -> FactView:
        self._drop_overlay_if_invalid()
        return self.active_view

    def reconcile_current_sources(self) -> IncrementalStatus:
        with self._notify_lock:
            return self._reconcile_current_sources()

    def notify_file_changed(self, path: Path) -> IncrementalStatus:
        with self._notify_lock:
            return self._notify_file_changed(path)

    def _reconcile_current_sources(self) -> IncrementalStatus:
        if not self.config.incremental_temporary_enabled:
            return self._status
        self._drop_overlay_if_invalid(validate_runtime=True)
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
        current_toolchain_hash = self._current_toolchain_hash()
        for entry in inventory:
            if entry.source_kind not in {"c_source", "header"}:
                continue
            source_path = self.target_repo / entry.rel_path
            try:
                stat = source_path.stat()
            except OSError:
                changed_entries.append(entry)
                for dirty in self._plan_dirty_sources(entry, inventory, None, reason="missing"):
                    dirty_by_source[dirty.source_id] = dirty
                continue
            compile_command_hash = self._current_compile_command_hash(entry)
            reason = None
            if compile_command_hash is not None and compile_command_hash != entry.compile_command_hash:
                reason = "compile_command_changed"
            if current_toolchain_hash is not None and current_toolchain_hash != entry.toolchain_hash:
                reason = "toolchain_changed"
            if reason is None and not _source_may_have_changed(entry, stat):
                continue
            try:
                current_sha256 = _file_sha256(source_path)
            except OSError:
                changed_entries.append(entry)
                for dirty in self._plan_dirty_sources(entry, inventory, None, reason="missing"):
                    dirty_by_source[dirty.source_id] = dirty
                continue
            self._observed_sha256_by_path[entry.rel_path] = current_sha256
            if reason is None and current_sha256 == entry.sha256:
                continue
            changed_entries.append(entry)
            if reason == "toolchain_changed":
                current_sha256 = entry.sha256
            elif reason == "compile_command_changed" and current_sha256 == entry.sha256:
                current_sha256 = entry.sha256
            else:
                reason = None
            for dirty in self._plan_dirty_sources(entry, inventory, current_sha256, reason=reason):
                existing = dirty_by_source.get(dirty.source_id)
                if existing is None or _dirty_source_priority(dirty.reason) < _dirty_source_priority(existing.reason):
                    dirty_by_source[dirty.source_id] = dirty

        dirty_sources = sorted(dirty_by_source.values(), key=lambda item: (item.rel_path, item.source_id))
        if not dirty_sources:
            self._status = IncrementalStatus("ready", base_snapshot_id)
            self._write_state(self._status)
            return self._status
        changed_source = changed_entries[0] if changed_entries else None
        if len(dirty_sources) > self.config.incremental_max_dirty_files:
            return self._publish_stale_warning(
                store,
                base_snapshot_id,
                dirty_sources,
                "dirty_set_too_large",
                changed_source=changed_source,
            )

        self._emit(
            "incremental.file_changed",
            "ok",
            counts={"changed_file_count": len(changed_entries)},
            payload={
                "source_id": changed_source.source_id if changed_source is not None else dirty_sources[0].source_id,
                "rel_path": changed_source.rel_path if changed_source is not None else dirty_sources[0].rel_path,
            },
        )
        return self._process_dirty_sources(store, base_snapshot_id, dirty_sources, started)

    def _notify_file_changed(self, path: Path) -> IncrementalStatus:
        if not self.config.incremental_temporary_enabled:
            return self._status
        self._drop_overlay_if_invalid(validate_runtime=True)
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
            return self._publish_stale_warning(
                store,
                base_snapshot_id,
                [DirtySource("unknown", rel_path, "compile_command_missing", None, None, 0)],
                "compile_command_missing",
            )
        compile_command_hash = self._current_compile_command_hash(entry)
        current_toolchain_hash = self._current_toolchain_hash()
        reason = None
        if compile_command_hash is not None and compile_command_hash != entry.compile_command_hash:
            reason = "compile_command_changed"
        if current_toolchain_hash is not None and current_toolchain_hash != entry.toolchain_hash:
            reason = "toolchain_changed"
        try:
            current_sha256 = _file_sha256(self.target_repo / rel_path)
        except OSError:
            dirty_sources = self._plan_dirty_sources(entry, inventory, None, reason="missing")
            self._emit(
                "incremental.file_changed",
                "ok",
                counts={"changed_file_count": 1},
                payload={"source_id": entry.source_id, "rel_path": rel_path},
            )
            return self._process_dirty_sources(store, base_snapshot_id, dirty_sources, started)
        self._observed_sha256_by_path[rel_path] = current_sha256
        self._emit(
            "incremental.file_changed",
            "ok",
            counts={"changed_file_count": 1},
            payload={"source_id": entry.source_id, "rel_path": rel_path},
        )
        if reason is None and current_sha256 == entry.sha256:
            if self._active_overlay is not None and entry.source_id in self._active_overlay.source_tombstones:
                self._drop_overlay("reverted_to_base")
            self._status = IncrementalStatus("ready", base_snapshot_id)
            self._write_state(self._status)
            return self._status
        if reason == "toolchain_changed":
            current_sha256 = entry.sha256
        elif reason == "compile_command_changed" and current_sha256 == entry.sha256:
            current_sha256 = entry.sha256
        else:
            reason = None
        dirty_sources = self._plan_dirty_sources(entry, inventory, current_sha256, reason=reason)
        if len(dirty_sources) > self.config.incremental_max_dirty_files:
            return self._publish_stale_warning(store, base_snapshot_id, dirty_sources, "dirty_set_too_large")
        return self._process_dirty_sources(store, base_snapshot_id, dirty_sources, started)

    def _process_dirty_sources(
        self,
        store,
        base_snapshot_id: Optional[str],
        dirty_sources: List[DirtySource],
        started: float,
    ) -> IncrementalStatus:
        if any(item.reason == "toolchain_changed" for item in dirty_sources):
            return self._publish_stale_warning(store, base_snapshot_id, dirty_sources, "toolchain_changed")
        self.generation += 1
        self._publish_base_state(store, "pending", base_snapshot_id, dirty_sources, pending_task_count=1)
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
            if all(item.reason == "missing" for item in dirty_sources):
                result = IncrementalBuildResult()
            else:
                result = self._extract_dirty(dirty_sources)
            stale_reason = self._result_stale_reason(result)
            if stale_reason is not None:
                return self._publish_stale_warning(store, base_snapshot_id, dirty_sources, stale_reason)
            return self._publish_overlay(store, base_snapshot_id, dirty_sources, result, started)
        except IncrementalError as exc:
            return self._fail(exc.code, base_snapshot_id, started, dirty_count=len(dirty_sources))
        except Exception:
            return self._fail("clang_ast_failed", base_snapshot_id, started, dirty_count=len(dirty_sources))

    def _normalize_changed_path(self, path: Path) -> str:
        target = self.target_repo.resolve(strict=False)
        resolved = Path(path).resolve(strict=False)
        if not _is_relative_to(resolved, target):
            raise IncrementalError("path_escape", "changed path escapes target repository")
        rel = resolved.relative_to(target).as_posix()
        if rel.startswith(".cipher/") or rel == ".cipher":
            raise IncrementalError("path_escape", "changed path cannot be inside .cipher")
        return rel

    def _plan_dirty_sources(
        self,
        entry: SourceInventoryEntry,
        inventory: List[SourceInventoryEntry],
        current_sha256: Optional[str],
        *,
        reason: Optional[str] = None,
    ) -> List[DirtySource]:
        if reason in {"missing", "compile_command_changed", "toolchain_changed"}:
            return [
                DirtySource(
                    source_id=entry.source_id,
                    rel_path=entry.rel_path,
                    reason=reason,
                    previous_sha256=entry.sha256,
                    current_sha256=current_sha256,
                    fanout_count=0,
                )
            ]
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
            extractor = CodeFactExtractor(self.target_repo, self.config, log_enabled=self.log_enabled)
        result = extractor.extract_dirty_sources(dirty_sources, self.profile)
        if isinstance(result, IncrementalBuildResult):
            return result
        facts = [fact.to_fact_record() if hasattr(fact, "to_fact_record") else fact for fact in result.facts]
        return IncrementalBuildResult(facts=facts, relatives=list(result.relatives), source_inventory=list(getattr(result, "source_inventory", [])))

    def _publish_base_state(
        self,
        store,
        state: str,
        base_snapshot_id: Optional[str],
        dirty_sources: List[DirtySource],
        *,
        pending_task_count: int = 0,
        latest_error_code: Optional[str] = None,
    ) -> IncrementalStatus:
        overlay = TemporaryOverlay(
            overlay_id=f"{state}-{self.generation}",
            view_state=state,
            stale_source_count=len(dirty_sources),
            pending_task_count=pending_task_count,
        )
        self._active_overlay = overlay
        self._overlay_guard = None
        self.active_view = store.open_view(overlay)
        self._status = IncrementalStatus(
            state,
            base_snapshot_id,
            overlay_id=overlay.overlay_id,
            dirty_source_count=len(dirty_sources),
            pending_task_count=pending_task_count,
            stale_source_count=len(dirty_sources),
            latest_error_code=latest_error_code,
        )
        self._write_state(self._status)
        return self._status

    def _publish_stale_warning(
        self,
        store,
        base_snapshot_id: Optional[str],
        dirty_sources: List[DirtySource],
        reason: str,
        *,
        changed_source: Optional[SourceInventoryEntry] = None,
    ) -> IncrementalStatus:
        if self.generation == 0 or self._status.state not in {"pending", "stale"}:
            self.generation += 1
        self._emit(
            "incremental.dirty_planned",
            "warning",
            counts={"dirty_source_count": len(dirty_sources), "fanout_count": sum(item.fanout_count for item in dirty_sources)},
            payload={"reason": reason, "base_snapshot_id": base_snapshot_id},
        )
        if changed_source is not None:
            self._emit(
                "incremental.file_changed",
                "ok",
                counts={"changed_file_count": 1},
                payload={"source_id": changed_source.source_id, "rel_path": changed_source.rel_path},
            )
        return self._publish_base_state(
            store,
            "stale",
            base_snapshot_id,
            dirty_sources,
            latest_error_code=reason,
        )

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
        overlay_id = f"overlay-{uuid.uuid4().hex[:16]}"
        overlay_dir = safe_cipher_path(self.target_repo, *RUN_DIR, "overlays", overlay_id)
        overlay_dir.mkdir(parents=True, exist_ok=True)
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
        self._overlay_guard = self._build_overlay_guard(store, overlay_id, dirty_sources)
        self.active_view = store.open_view(overlay)
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
                "guard_fingerprint": self._guard_fingerprint(self._overlay_guard),
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

    def _drop_overlay(self, reason: str, *, status: str = "ok") -> None:
        self._emit(
            "incremental.overlay_dropped",
            status,
            counts={"dropped_overlay_count": 1},
            payload={"reason": reason},
        )
        run_dir = safe_cipher_path(self.target_repo, *RUN_DIR)
        overlays = run_dir / "overlays"
        if overlays.exists():
            shutil.rmtree(overlays, ignore_errors=True)
        self._active_overlay = None
        self._overlay_guard = None
        self.active_view = open_fact_store(self.target_repo, mode="r", log_enabled=False).open_view(None)
        self._status = IncrementalStatus("ready", self.active_view.base_snapshot_id)
        self._write_state(self._status)

    def _fail(
        self,
        code: str,
        base_snapshot_id: Optional[str],
        started: float,
        *,
        dirty_count: int = 0,
    ) -> IncrementalStatus:
        self._active_overlay = None
        self._overlay_guard = None
        self.active_view = open_fact_store(self.target_repo, mode="r", log_enabled=False).open_view(None)
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

    def _write_state(self, status: IncrementalStatus) -> None:
        path = safe_cipher_path(self.target_repo, *RUN_DIR, STATE_FILENAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status.to_json(), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

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

    def _drop_overlay_if_invalid(self, *, validate_runtime: bool = False) -> None:
        guard = self._overlay_guard
        overlay = self._active_overlay
        if guard is None or overlay is None or overlay.view_state != "overlay":
            return
        now = time.monotonic()
        if now - guard.last_access_monotonic > self.config.incremental_overlay_ttl_seconds:
            self._drop_overlay("ttl_expired", status="warning")
            return
        reason = self._overlay_guard_invalid_reason(guard, validate_runtime=validate_runtime)
        if reason is not None:
            self._drop_overlay(reason, status="warning")
            return
        guard.last_access_monotonic = now

    def _overlay_guard_invalid_reason(
        self,
        guard: OverlayRuntimeGuard,
        *,
        validate_runtime: bool,
    ) -> Optional[str]:
        if not validate_runtime:
            current_snapshot_id = _read_current_snapshot_id(self.target_repo)
            if current_snapshot_id != guard.base_snapshot_id:
                return "base_snapshot_changed"
            current_toolchain_hash = self._toolchain_hash_cache if self._toolchain_hash_cache_ready else None
            if current_toolchain_hash is not None:
                for expected in guard.source_toolchain_hashes.values():
                    if current_toolchain_hash != expected:
                        return "toolchain_changed"
            return None
        try:
            store = open_fact_store(self.target_repo, mode="r", log_enabled=False)
            stats = store.stats()
        except StorageError:
            return "storage_schema_changed"
        if stats.snapshot_id != guard.base_snapshot_id:
            return "base_snapshot_changed"
        if _storage_schema_fingerprint(stats) != guard.storage_schema_fingerprint:
            return "storage_schema_changed"
        try:
            inventory_by_id = {entry.source_id: entry for entry in store.iter_source_inventory()}
        except StorageError:
            return "base_snapshot_changed"
        for source_id, expected in guard.source_compile_command_hashes.items():
            entry = inventory_by_id.get(source_id)
            if entry is None:
                return "compile_command_changed"
            current = self._current_compile_command_hash(entry)
            if current is not None and current != expected:
                return "compile_command_changed"
            if current is None and entry.compile_command_hash != expected:
                return "compile_command_changed"
        current_toolchain_hash = self._current_toolchain_hash()
        for source_id, expected in guard.source_toolchain_hashes.items():
            entry = inventory_by_id.get(source_id)
            if entry is None:
                return "toolchain_changed"
            if current_toolchain_hash is not None and current_toolchain_hash != expected:
                return "toolchain_changed"
            if current_toolchain_hash is None and entry.toolchain_hash != expected:
                return "toolchain_changed"
        return None

    def _build_overlay_guard(
        self,
        store,
        overlay_id: str,
        dirty_sources: List[DirtySource],
    ) -> OverlayRuntimeGuard:
        stats = store.stats()
        dirty_source_ids = {item.source_id for item in dirty_sources}
        inventory_by_id = {
            entry.source_id: entry
            for entry in store.iter_source_inventory()
            if entry.source_id in dirty_source_ids
        }
        current_toolchain_hash = self._current_toolchain_hash()
        compile_command_hashes: Dict[str, Optional[str]] = {}
        toolchain_hashes: Dict[str, str] = {}
        for source_id, entry in sorted(inventory_by_id.items()):
            current_compile_command_hash = self._current_compile_command_hash(entry)
            compile_command_hashes[source_id] = (
                current_compile_command_hash
                if current_compile_command_hash is not None
                else entry.compile_command_hash
            )
            toolchain_hashes[source_id] = current_toolchain_hash or entry.toolchain_hash
        now = time.monotonic()
        return OverlayRuntimeGuard(
            overlay_id=overlay_id,
            base_snapshot_id=stats.snapshot_id,
            storage_schema_fingerprint=_storage_schema_fingerprint(stats),
            source_compile_command_hashes=compile_command_hashes,
            source_toolchain_hashes=toolchain_hashes,
            published_at_monotonic=now,
            last_access_monotonic=now,
        )

    def _guard_fingerprint(self, guard: Optional[OverlayRuntimeGuard]) -> Optional[str]:
        if guard is None:
            return None
        return _hash_json(
            {
                "overlay_id": guard.overlay_id,
                "base_snapshot_id": guard.base_snapshot_id,
                "storage_schema_fingerprint": guard.storage_schema_fingerprint,
                "source_compile_command_hashes": guard.source_compile_command_hashes,
                "source_toolchain_hashes": guard.source_toolchain_hashes,
            }
        )[:16]

    def _result_stale_reason(self, result: IncrementalBuildResult) -> Optional[str]:
        current_toolchain_hash = self._current_toolchain_hash()
        if current_toolchain_hash is None:
            return None
        for entry in result.source_inventory:
            if entry.toolchain_hash != current_toolchain_hash:
                return "toolchain_changed"
        return None

    def _current_compile_command_hash(self, entry: SourceInventoryEntry) -> Optional[str]:
        if entry.source_kind != "c_source" or self.config.compile_database_path is None:
            return None
        path = self.config.compile_database_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if not isinstance(data, list):
            return ""
        target_resolved = self.target_repo.resolve(strict=False)
        for item in data:
            if not isinstance(item, dict):
                return ""
            try:
                compile_entry = _compile_command_entry_from_mapping(self.target_repo, path, item)
            except _MalformedCompileDatabaseError:
                return ""
            if compile_entry is None:
                continue
            rel_source = compile_entry.source_path.relative_to(target_resolved).as_posix()
            if rel_source == entry.rel_path:
                return compile_entry.command_hash
        return ""

    def _current_toolchain_hash(self) -> Optional[str]:
        if not self._toolchain_hash_cache_ready:
            self._toolchain_hash_cache = self._compute_toolchain_hash()
            self._toolchain_hash_cache_ready = True
        return self._toolchain_hash_cache

    def _compute_toolchain_hash(self) -> Optional[str]:
        if self._extractor is not None:
            return None
        if not any((self.config.clang_executable, self.config.gcc_executable, self.config.libclang_library_path)):
            return None
        extractor = CodeFactExtractor(self.target_repo, self.config, log_enabled=False)
        try:
            extractor._validate_toolchain()
        except Exception:
            return None
        return _hash_json(
            {
                "clang": self.config.clang_executable,
                "gcc": self.config.gcc_executable,
                "clang_args": self.config.clang_args,
                "profile": self.profile,
                "toolchain_probe": extractor.toolchain_probe_result_to_digest(),
            }
        )

    def _prime_observed_sources(self) -> None:
        try:
            inventory = list(open_fact_store(self.target_repo, mode="r", log_enabled=False).iter_source_inventory())
        except StorageError:
            return
        for entry in inventory:
            self._observed_sha256_by_path.setdefault(entry.rel_path, entry.sha256)

    def _poll_loop(self) -> None:
        interval = self.config.incremental_poll_interval_ms / 1000.0
        while not self._stop_event.wait(interval):
            self._scan_once()

    def _scan_once(self) -> None:
        if not self.config.incremental_temporary_enabled:
            return
        self._drop_overlay_if_invalid(validate_runtime=True)
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
                self.notify_file_changed(source_path)
                return
            observed_sha256 = self._observed_sha256_by_path.setdefault(entry.rel_path, entry.sha256)
            if current_sha256 == observed_sha256:
                continue
            if self._stop_event.wait(self.config.incremental_debounce_ms / 1000.0):
                return
            status = self.notify_file_changed(source_path)
            if status.state in {"overlay", "ready", "error"}:
                self._observed_sha256_by_path[entry.rel_path] = current_sha256
            return


def read_incremental_status(target_repo: Path) -> IncrementalStatus:
    path = Path(target_repo) / ".cipher" / "run" / "incremental" / STATE_FILENAME
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


def _hash_json(payload: Dict[str, JSONValue]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _storage_schema_fingerprint(stats) -> str:
    return _hash_json(
        {
            "snapshot_format": stats.snapshot_format,
            "compression": stats.compression,
            "read_index_state": stats.read_index_state,
            "read_index_schema_version": stats.read_index_schema_version,
            "read_index_codec": stats.read_index_codec,
        }
    )


def _read_current_snapshot_id(target_repo: Path) -> Optional[str]:
    try:
        value = (Path(target_repo) / ".cipher" / "snapshots" / "current").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


__all__ = [
    "DirtySource",
    "IncrementalBuildResult",
    "IncrementalCoordinator",
    "IncrementalError",
    "IncrementalStatus",
    "read_incremental_status",
]

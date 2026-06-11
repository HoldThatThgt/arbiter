"""Lazy writer-gated facts overlay view state."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from arbiter_engine import errors
from arbiter_engine.facts import relocation
from arbiter_engine.shared import census
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
    return relocation.facts_dir(repo) / "overlay" / "current.json"


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
    with locks.acquire(repo, [locks.OVERLAY], timeout_s=timeout_s):
        current = census.scan(repo, ["*", "**/*"])
        base_snapshot_id = _base_snapshot_id(repo)
        view = FactView(
            view_state="overlay",
            base_snapshot_id=base_snapshot_id,
            overlay_id="overlay:" + current.digest[:16],
        )
        _write_overlay_state(repo, view, current.digest)
        return view


def read_published(repo: Path) -> FactView:
    path = overlay_state_path(Path(repo))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return FactView(view_state="base", base_snapshot_id=_base_snapshot_id(Path(repo)), overlay_id=None)
    overlay_id = raw.get("overlay_id")
    base_snapshot_id = raw.get("base_snapshot_id")
    if not isinstance(overlay_id, str) or not overlay_id:
        return FactView(view_state="base", base_snapshot_id=_base_snapshot_id(Path(repo)), overlay_id=None)
    if base_snapshot_id is not None and not isinstance(base_snapshot_id, str):
        base_snapshot_id = None
    return FactView(view_state="overlay", base_snapshot_id=base_snapshot_id, overlay_id=overlay_id)


def _is_writer(context: AccessContext) -> bool:
    return context.role == "QUERY" and context.seat == "player"


def _write_overlay_state(repo: Path, view: FactView, digest: str) -> None:
    path = overlay_state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "base_snapshot_id": view.base_snapshot_id,
                "overlay_id": view.overlay_id,
                "view_state": view.view_state,
                "digest": digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


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
            return current.name
    except OSError:
        return None
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

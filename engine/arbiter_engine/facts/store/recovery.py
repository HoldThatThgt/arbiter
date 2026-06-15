"""Explicit storage recovery helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..relocation import facts_dir


def force_unlock(target_repo: Path) -> bool:
    # Single source of truth for the store root (same helper FileFactStore uses),
    # so this can never drift from the live lock again (was a stale cipher-2 ".cipher").
    lock_dir = facts_dir(target_repo) / "run" / "storage.lock"
    if not lock_dir.exists():
        return False
    owner_path = lock_dir / "owner.json"
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    pid = owner.get("pid")
    if isinstance(pid, int) and _pid_exists(pid):
        return False
    for child in lock_dir.iterdir():
        child.unlink()
    lock_dir.rmdir()
    return True


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

__all__ = [name for name in globals() if not name.startswith("__")]

"""Repo-local flock inventory and ordered acquisition helpers."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from arbiter_engine import errors


@dataclass(frozen=True)
class LockSpec:
    name: str
    label: str
    order: int
    key: str = ""


MATCH = LockSpec("match", "match.lock", 10)
SNAPSHOT = LockSpec("snapshot", "snapshot.lock", 20)
OVERLAY = LockSpec("overlay", "overlay.lock", 30)
STATE = LockSpec("state", "state.lock", 40)


def build_lock(workdir: Path | str) -> LockSpec:
    raw = os.path.abspath(os.fspath(workdir)).encode("utf-8", "surrogateescape")
    key = hashlib.sha256(raw).hexdigest()[:8]
    return LockSpec("build", f"build/{key}.lock", 50, key)


def path_for(root: Path | str, spec: LockSpec) -> Path:
    base = Path(root) / ".arbiter" / "locks"
    if spec.name == "build":
        if not spec.key:
            raise ValueError("build lock requires a key")
        return base / "build" / f"{spec.key}.lock"
    return base / spec.label


def _assert_order(specs: Sequence[LockSpec]) -> None:
    previous = 0
    for spec in specs:
        assert spec.order > previous, "lock order violation"
        previous = spec.order


@contextlib.contextmanager
def acquire(
    root: Path | str,
    specs: Sequence[LockSpec],
    *,
    timeout_s: float,
) -> Iterator[None]:
    _assert_order(specs)
    held: list[tuple[int, object]] = []
    try:
        deadline = time.monotonic() + timeout_s
        for spec in specs:
            path = path_for(root, spec)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            fd = handle.fileno()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append((fd, handle))
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        handle.close()
                        raise errors.lock_timeout(spec.label)
                    time.sleep(0.01)
        yield
    finally:
        while held:
            fd, handle = held.pop()
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                handle.close()

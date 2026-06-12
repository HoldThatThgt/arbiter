"""Census-validated build cache."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from arbiter_engine.runs import state
from arbiter_engine.shared import census


@dataclass(frozen=True)
class BuildCacheEntry:
    key: str
    sources_digest: str
    binary: str
    built_at: float


def store(
    db_path: Path | str,
    repo_root: Path | str,
    *,
    key: str,
    binary: str,
    sources: Sequence[str],
) -> BuildCacheEntry:
    digest = _sources_digest(repo_root, sources) if sources else ""
    built_at = time.time()
    with state.transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO compile_cache (key, sources_digest, binary, built_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                sources_digest=excluded.sources_digest,
                binary=excluded.binary,
                built_at=excluded.built_at
            """,
            (key, digest, binary, built_at),
        )
    return BuildCacheEntry(key=key, sources_digest=digest, binary=binary, built_at=built_at)


def lookup(
    db_path: Path | str,
    repo_root: Path | str,
    *,
    key: str,
    sources: Sequence[str],
) -> BuildCacheEntry | None:
    if not sources:
        return None
    state.init(db_path)
    with state.connect(db_path) as conn:
        row = conn.execute(
            "SELECT sources_digest, binary, built_at FROM compile_cache WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    expected_digest, binary, built_at = row
    if not _binary_exists(repo_root, binary):
        return None
    current_digest = _sources_digest(repo_root, sources)
    if current_digest != expected_digest:
        return None
    return BuildCacheEntry(
        key=key,
        sources_digest=expected_digest,
        binary=binary,
        built_at=built_at,
    )


def _sources_digest(repo_root: Path | str, sources: Sequence[str]) -> str:
    return census.scan(Path(repo_root), sources).digest


def _binary_exists(repo_root: Path | str, binary: str) -> bool:
    path = Path(binary)
    if not path.is_absolute():
        path = Path(repo_root) / path
    return path.exists()

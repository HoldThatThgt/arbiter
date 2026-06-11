"""Work-tree census over relative scope globs."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class CensusEntry:
    path: str
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class CensusResult:
    digest: str
    files: Mapping[str, CensusEntry]
    new: List[str]
    deleted: List[str]
    changed: List[str]


def scan(root: Path, globs: Iterable[str], previous: Optional[CensusResult] = None) -> CensusResult:
    root = Path(root)
    patterns = tuple(globs)
    previous_files: Mapping[str, CensusEntry] = {} if previous is None else previous.files
    files: Dict[str, CensusEntry] = {}
    new = []
    changed = []

    for relpath, path in _walk_files(root, patterns):
        stat = path.stat()
        old = previous_files.get(relpath)
        if old is not None and old.size == stat.st_size and old.mtime_ns == stat.st_mtime_ns:
            entry = CensusEntry(relpath, stat.st_size, stat.st_mtime_ns, old.sha256)
        else:
            digest = _sha256_file(path)
            entry = CensusEntry(relpath, stat.st_size, stat.st_mtime_ns, digest)
            if old is None:
                new.append(relpath)
            elif old.sha256 != digest:
                changed.append(relpath)
        files[relpath] = entry

    deleted = sorted(path for path in previous_files if path not in files)
    ordered_files = {path: files[path] for path in sorted(files)}
    return CensusResult(
        digest=_digest(ordered_files),
        files=ordered_files,
        new=sorted(new),
        deleted=deleted,
        changed=sorted(changed),
    )


def to_json(result: CensusResult) -> dict:
    return {
        "digest": result.digest,
        "files": {
            path: {
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
                "sha256": entry.sha256,
            }
            for path, entry in result.files.items()
        },
        "new": list(result.new),
        "deleted": list(result.deleted),
        "changed": list(result.changed),
    }


def from_json(payload: Mapping[str, object]) -> CensusResult:
    raw_files = payload.get("files", {})
    if not isinstance(raw_files, dict):
        raise ValueError("previous.files must be an object")
    files: Dict[str, CensusEntry] = {}
    for path, raw_entry in raw_files.items():
        if not isinstance(path, str) or not isinstance(raw_entry, dict):
            raise ValueError("previous.files entries are invalid")
        size = raw_entry.get("size")
        mtime_ns = raw_entry.get("mtime_ns")
        digest = raw_entry.get("sha256")
        if not isinstance(size, int) or not isinstance(mtime_ns, int) or not isinstance(digest, str):
            raise ValueError("previous.files entries are invalid")
        files[path] = CensusEntry(path, size, mtime_ns, digest)
    digest_value = payload.get("digest", "")
    if not isinstance(digest_value, str):
        raise ValueError("previous.digest must be a string")
    return CensusResult(digest=digest_value, files=files, new=[], deleted=[], changed=[])


def _walk_files(root: Path, patterns: Tuple[str, ...]) -> Iterable[Tuple[str, Path]]:
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in {".git", ".arbiter"})
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            relpath = path.relative_to(root).as_posix()
            if _matches(relpath, patterns):
                matches.append((relpath, path))
    return matches


def _matches(relpath: str, patterns: Tuple[str, ...]) -> bool:
    parts = relpath.split("/")
    return any(_match_segments(parts, pattern.split("/")) for pattern in patterns)


def _match_segments(parts: Sequence[str], pattern: Sequence[str]) -> bool:
    """Anchored glob match where '**' spans zero or more path segments."""
    if not pattern:
        return not parts
    head = pattern[0]
    if head == "**":
        # '**' matches zero segments ...
        if _match_segments(parts, pattern[1:]):
            return True
        # ... or one segment followed by the same pattern again.
        return bool(parts) and _match_segments(parts[1:], pattern)
    if not parts:
        return False
    if not fnmatch.fnmatchcase(parts[0], head):
        return False
    return _match_segments(parts[1:], pattern[1:])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(files: Mapping[str, CensusEntry]) -> str:
    digest = hashlib.sha256()
    for path, entry in files.items():
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()

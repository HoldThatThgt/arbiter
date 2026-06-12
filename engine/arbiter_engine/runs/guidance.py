"""Failure guidance from a stub facts read_index."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, Tuple


MAX_GUIDANCE = 4


@dataclass(frozen=True)
class GuidanceEntry:
    test: str
    file: str
    line: int
    next_queries: Tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "test": self.test,
            "file": self.file,
            "line": self.line,
            "next_queries": list(self.next_queries),
        }


def for_result(repo_root: Path | str, result: Any) -> Tuple[GuidanceEntry, ...]:
    if getattr(result, "overall", "") != "failed":
        return ()
    index = _load_index(Path(repo_root))
    if not index:
        return ()
    entries = []
    for test in getattr(result, "per_test", ()):
        if getattr(test, "status", "") != "failed":
            continue
        test_name = _test_name(getattr(test, "suite", ""), getattr(test, "name", ""))
        hit = index.get(test_name)
        if hit is None:
            continue
        detail_id = hit.get("detail_id")
        queries = []
        if isinstance(detail_id, str) and detail_id:
            queries.append(f"detail {detail_id}")
        queries.append(f'search "test:{test_name}"')
        entries.append(
            GuidanceEntry(
                test=test_name,
                file=str(hit["file"]),
                line=int(hit["line"]),
                next_queries=tuple(queries),
            )
        )
        if len(entries) >= MAX_GUIDANCE:
            break
    return tuple(entries)


def _load_index(root: Path) -> Mapping[str, Mapping[str, Any]]:
    path = root / ".arbiter" / "facts" / "read_index.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    tests = payload.get("tests") if isinstance(payload, dict) else None
    if not isinstance(tests, list):
        return {}
    index = {}
    for item in tests:
        if not isinstance(item, dict):
            continue
        suite = item.get("suite")
        name = item.get("name")
        file = item.get("file")
        line = item.get("line")
        if not isinstance(suite, str) or not isinstance(name, str):
            continue
        if not isinstance(file, str) or not isinstance(line, int):
            continue
        index[_test_name(suite, name)] = item
    return index


def _test_name(suite: str, name: str) -> str:
    return f"{suite}.{name}" if suite else name

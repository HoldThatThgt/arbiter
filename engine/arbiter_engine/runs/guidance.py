"""Failure guidance resolved against the real facts read_index.

On a failed run, each failing gtest case is resolved against the published facts
read_index (the same TestBody function facts ``runs.scan`` discovers) to hand the
model a copy-paste ``detail``/``search`` next-query with the test's file:line — the
red-test -> facts loop. Fail-closed: when no snapshot exists (the index is absent or
empty) there is nothing to resolve, so guidance is empty rather than a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from arbiter_engine.runs import discovery


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
        detail_id = hit.get("fact_id")
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
    # Resolve failing-test symbols against the real facts read_index (TestBody
    # function facts), not a side-loaded JSON stub. discover_test_candidates is
    # itself fail-closed, so a cold repo yields an empty mapping here.
    index: Dict[str, Mapping[str, Any]] = {}
    for candidate in discovery.discover_test_candidates(root):
        index[candidate.test] = {
            "file": candidate.file,
            "line": candidate.line,
            "fact_id": candidate.fact_id,
        }
    return index


def _test_name(suite: str, name: str) -> str:
    return f"{suite}.{name}" if suite else name

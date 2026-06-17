"""Facts-derived test discovery for `runs.scan`.

The primary (and, in this stdlib-only engine, the *only*) test-discovery path is a
query against the real facts read_index for the **gtest fixture types** a green build
publishes — ``TEST(Suite, Name)`` / ``TEST_F`` / ``TEST_P`` generate a ``Suite_Name_Test``
class, and the libclang extractor records that as a ``type`` fact (it does NOT emit the
macro-expanded ``::TestBody`` method), so a published snapshot carries one ``_Test`` type
fact per test case. ``scan`` reads them back as candidate targets to register and prove;
there is no tree-sitter fallback here (it lives behind the optional ``[scan]`` extra), so
when no snapshot exists / the index is empty we return an empty, typed result —
fail-closed, never a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from arbiter_engine.facts import store as facts_store
from arbiter_engine.runs import state as run_state


# gtest test bodies all share this method name; querying the read_index for it pulls
# back exactly the function facts a TEST/TEST_F/TEST_P macro produced.
_TEST_BODY_SUFFIX = "::TestBody"
_TEST_CLASS_SUFFIX = "_Test"
# Upper bound on facts pulled from the index for one scan. Test suites are small
# relative to the whole index; this keeps a pathological repo from streaming the
# entire fact set through the (already index-backed) search.
_SCAN_QUERY_LIMIT = 1000


@dataclass(frozen=True)
class TestCandidate:
    """A discovered gtest test case to register and prove."""

    suite: str
    name: str
    file: str
    line: int
    fact_id: str

    @property
    def test(self) -> str:
        """gtest filter form (``Suite.Name``) the run/recipe tools consume."""
        return f"{self.suite}.{self.name}" if self.suite else self.name

    def to_json(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "name": self.name,
            "test": self.test,
            "file": self.file,
            "line": self.line,
            "fact_id": self.fact_id,
        }


def discover_test_candidates(
    repo_root: Path | str,
    *,
    limit: int = _SCAN_QUERY_LIMIT,
) -> Tuple[TestCandidate, ...]:
    """Query the real facts read_index for TestBody function facts.

    Fail-closed: if the facts store has no published snapshot (the read_index is
    absent or empty) this returns an empty tuple instead of raising, so a cold repo
    or a storage hiccup degrades to "nothing to register" rather than crashing the
    scan tool.
    """
    root = Path(repo_root)
    try:
        store = facts_store.open_fact_store(root, mode="r")
        # search() routes through the persisted sqlite read_index; on a cold repo
        # the index is absent and the store returns [] (no snapshot ⇒ no facts).
        facts = store.search("_Test", limit)
    except facts_store.StorageError:
        return ()
    candidates: List[TestCandidate] = []
    seen: set[Tuple[str, str]] = set()
    for fact in facts:
        candidate = _candidate_from_fact(fact)
        if candidate is None:
            continue
        key = (candidate.suite, candidate.name)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    candidates.sort(key=lambda candidate: (candidate.suite, candidate.name))
    return tuple(candidates)


def scan(
    repo_root: Path | str,
    scope: str,
    *,
    state_path: Optional[Path] = None,
) -> Tuple[TestCandidate, ...]:
    """Run discovery for ``scope`` and round-trip it through ``scanned_test``.

    The discovered candidates are persisted under a scope-derived ``target_id`` and
    then read back, so the result reflects exactly what landed in the (previously
    created-but-unused) ``scanned_test`` table — the table is now live state, not
    dead schema.
    """
    candidates = discover_test_candidates(repo_root)
    db_path = state_path if state_path is not None else _state_path(Path(repo_root))
    target_id = scan_target_id(scope)
    run_state.replace_scanned_tests(
        db_path,
        target_id,
        (
            run_state.ScannedTest(
                target_id=target_id,
                suite=candidate.suite,
                name=candidate.name,
                file=candidate.file,
                line=candidate.line,
            )
            for candidate in candidates
        ),
    )
    persisted = run_state.read_scanned_tests(db_path, target_id)
    fact_by_key = {(candidate.suite, candidate.name): candidate.fact_id for candidate in candidates}
    return tuple(
        TestCandidate(
            suite=row.suite,
            name=row.name,
            file=row.file,
            line=row.line,
            fact_id=fact_by_key.get((row.suite, row.name), ""),
        )
        for row in persisted
    )


def scan_target_id(scope: str) -> str:
    """Stable ``scanned_test`` partition key for a scan ``scope``."""
    cleaned = scope.strip()
    return f"scan:{cleaned}" if cleaned else "scan:*"


def _state_path(repo_root: Path) -> Path:
    return repo_root / ".arbiter" / "runs" / "state.sqlite"


def _candidate_from_fact(fact: Any) -> Optional[TestCandidate]:
    if _fact_kind(fact) != "type":
        return None
    suite, name = _suite_and_name(fact)
    if suite is None or name is None:
        return None
    file, line = _file_and_line(fact)
    return TestCandidate(
        suite=suite,
        name=name,
        file=file,
        line=line,
        fact_id=getattr(fact, "object_id", "") or "",
    )


def _fact_kind(fact: Any) -> str:
    payload = getattr(fact, "payload", None)
    if isinstance(payload, Mapping):
        value = payload.get("fact_kind")
        if isinstance(value, str) and value:
            return value
    return "fact"


def _suite_and_name(fact: Any) -> Tuple[Optional[str], Optional[str]]:
    # Explicit payload metadata is authoritative when present — gtest suite/name can
    # both contain underscores, which makes name-parsing of Suite_Name_Test ambiguous.
    payload = getattr(fact, "payload", None)
    if isinstance(payload, Mapping):
        suite = payload.get("test_suite")
        name = payload.get("test_name")
        if isinstance(suite, str) and suite and isinstance(name, str) and name:
            return suite, name
    return _parse_test_body_name(getattr(fact, "object_name", None))


def _parse_test_body_name(object_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    # The libclang extractor records the gtest-generated test FIXTURE TYPE
    # (TEST(Suite, Name) → `Suite_Name_Test`), not the macro-expanded `::TestBody`
    # method, so discovery keys off the `_Test` type name.
    if not isinstance(object_name, str) or not object_name.endswith(_TEST_CLASS_SUFFIX):
        return None, None
    stem = object_name[: -len(_TEST_CLASS_SUFFIX)]
    suite, separator, name = stem.partition("_")
    if not separator or not suite or not name:
        return None, None
    return suite, name


def _file_and_line(fact: Any) -> Tuple[str, int]:
    source = getattr(fact, "object_source", None)
    if not isinstance(source, str) or not source:
        return "<unknown-source>", 0
    path, separator, line = source.rpartition(":")
    if separator and path and line.isdigit():
        return path, int(line)
    return source, 0


__all__ = [
    "TestCandidate",
    "discover_test_candidates",
    "scan",
    "scan_target_id",
]

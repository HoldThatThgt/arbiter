"""Test discovery for the ``scan`` tool.

Two complementary sources feed discovery:

* **facts** (``discover_test_candidates``) — a query against the real facts
  read_index for the **gtest fixture types** a green build publishes:
  ``TEST(Suite, Name)`` / ``TEST_F`` / ``TEST_P`` generate a ``Suite_Name_Test``
  class, and the libclang extractor records that as a ``type`` fact (it does NOT
  emit the macro-expanded ``::TestBody`` method), so a published snapshot carries
  one ``_Test`` type fact per test case that actually COMPILED and was indexed.
* **source AST** (``runs.scan``) — a build-independent tree-sitter walk of the
  C++ sources that finds every test DECLARED in source, whether or not it built.

``scan`` UNIONS the two by ``(suite, name)``: every declared test is reported,
and a non-empty ``fact_id`` marks the ones the build has actually proven. The AST
half lives behind the optional ``[scan]`` extra; when tree-sitter is not
installed ``scan`` degrades to the facts-only inventory (and the tool surfaces a
typed reason). A cold repo with neither snapshot nor sources yields an empty,
typed result — fail-closed, never a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from arbiter_engine.facts import store as facts_store
from arbiter_engine.runs import scan as ast_scan
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
    # The declaring macro (TEST/TEST_F/TEST_P/...) when the AST scan found it;
    # empty for a facts-only candidate, which carries no macro-kind metadata.
    kind: str = ""
    # The fixture class for TEST_F/TEST_P; None for a plain TEST or facts-only.
    fixture: Optional[str] = None

    @property
    def test(self) -> str:
        """gtest filter form (``Suite.Name``) the run/recipe tools consume."""
        return f"{self.suite}.{self.name}" if self.suite else self.name

    @property
    def built(self) -> bool:
        """True when the build proved this test (a real fact backs it)."""
        return bool(self.fact_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "name": self.name,
            "test": self.test,
            "file": self.file,
            "line": self.line,
            "fact_id": self.fact_id,
            "kind": self.kind,
            "built": self.built,
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


def discover_declared_tests(repo_root: Path | str) -> Tuple[TestCandidate, ...]:
    """Build-independent AST discovery of every test DECLARED in source.

    Delegates to the optional tree-sitter scanner (``runs.scan``). When that extra
    is not installed this returns an empty tuple — callers fall back to the
    facts-only inventory rather than failing.
    """
    try:
        declared = ast_scan.scan_sources(repo_root)
    except ast_scan.ScanUnavailable:
        return ()
    candidates = [
        TestCandidate(
            suite=test.suite,
            name=test.name,
            file=test.file,
            line=test.line,
            fact_id="",
            kind=test.kind,
            fixture=test.fixture,
        )
        for test in declared
    ]
    candidates.sort(key=lambda candidate: (candidate.suite, candidate.name))
    return tuple(candidates)


# Bundled third-party trees declare their own gtest cases (e.g. a vendored abseil
# or googletest under extra/), which a project bootstrap is not responsible for
# covering. Coverage is measured over the PROJECT scope only — these prefixes are
# excluded from the denominator so "build every test binary" is an achievable bar.
# (The scan inventory itself stays complete; only the coverage ratio is scoped.)
_VENDOR_PREFIXES = (
    "extra/", "third_party/", "third-party/", "thirdparty/", "vendor/",
    "external/", "deps/", "contrib/", "node_modules/",
)


def _in_project_scope(file: str) -> bool:
    return not any(file.startswith(prefix) for prefix in _VENDOR_PREFIXES)


def coverage(repo_root: Path | str, *, limit: int = 200_000) -> dict[str, Any]:
    """Per-BINARY build coverage over the project scope: built test files / declared test files.

    Coverage is measured at FILE granularity — one test source file is the unit of a
    test binary, so each binary counts once regardless of how many cases it holds. A
    file counts as ``built`` when the facts index carries at least one of its declared
    gtest cases (running even a few of a binary's tests builds + indexes that file).
    This weights every binary equally: a couple of huge binaries cannot satisfy the
    bar while many small ones stay uncovered.

    ``declared`` files come from the build-INDEPENDENT AST scan; ``built`` is produced
    ONLY by a real ``arbiter cc``-interposed build, so the ratio cannot be faked.
    Vendored third-party trees are excluded. Proving one binary scores ~0; covering
    the binaries drives it to ~1. ``*_tests`` are reported for context only.
    """
    declared = [t for t in discover_declared_tests(repo_root) if _in_project_scope(t.file)]
    built_keys = {
        (candidate.suite, candidate.name)
        for candidate in discover_test_candidates(repo_root, limit=limit)
    }
    declared_files = {t.file for t in declared}
    built_files = {t.file for t in declared if (t.suite, t.name) in built_keys}
    n_declared = len(declared_files)
    n_built = len(built_files)
    ratio = (n_built / n_declared) if n_declared else 0.0
    return {
        "declared": n_declared,
        "built": n_built,
        "ratio": round(ratio, 4),
        "declared_tests": len(declared),
        "built_tests": sum(1 for t in declared if (t.suite, t.name) in built_keys),
    }


def _union(repo_root: Path | str) -> Tuple[TestCandidate, ...]:
    """Merge AST-declared tests with facts-built tests, keyed by (suite, name).

    Declared tests carry the macro kind and authoritative source location; the
    facts overlay attaches ``fact_id`` to the ones the build actually proved.
    Tests present only in facts (e.g. macro-generated cases the AST cannot see)
    are still included, so the union is a superset of either source alone.
    """
    merged: Dict[Tuple[str, str], TestCandidate] = {}
    for candidate in discover_declared_tests(repo_root):
        merged[(candidate.suite, candidate.name)] = candidate
    for built in discover_test_candidates(repo_root):
        key = (built.suite, built.name)
        declared = merged.get(key)
        if declared is None:
            merged[key] = built
        else:
            # Keep the AST's kind/fixture/location; mark it proven via fact_id.
            merged[key] = TestCandidate(
                suite=declared.suite,
                name=declared.name,
                file=declared.file,
                line=declared.line,
                fact_id=built.fact_id,
                kind=declared.kind,
                fixture=declared.fixture,
            )
    return tuple(sorted(merged.values(), key=lambda c: (c.suite, c.name)))


def scan(
    repo_root: Path | str,
    scope: str,
    *,
    state_path: Optional[Path] = None,
) -> Tuple[TestCandidate, ...]:
    """Run discovery for ``scope`` and round-trip it through ``scanned_test``.

    Discovery unions the build-independent AST scan with the facts index (see
    ``_union``). The result is persisted under a scope-derived ``target_id`` and
    then read back, so it reflects exactly what landed in the ``scanned_test``
    table — the table is live state, not dead schema. Full per-candidate metadata
    (``fact_id``, ``kind``, ``fixture``) is re-attached from the in-memory union,
    since the table persists only the stable identity columns.
    """
    candidates = _union(repo_root)
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
    by_key = {(candidate.suite, candidate.name): candidate for candidate in candidates}
    return tuple(
        by_key.get(
            (row.suite, row.name),
            TestCandidate(suite=row.suite, name=row.name, file=row.file, line=row.line, fact_id=""),
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
    "discover_declared_tests",
    "discover_test_candidates",
    "scan",
    "scan_target_id",
]

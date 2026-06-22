"""Build-independent GoogleTest discovery via a tree-sitter C++ AST walk.

``runs.discovery`` queries the published facts index for the gtest fixture types
a *green, cc-interposed* build emits — so it only ever sees tests that actually
compiled and were indexed. This module is the complementary half: it parses the
project's C++ sources directly (no build required) and reports every test
DECLARED in source, whether or not it has been built. ``runs.discovery.scan``
unions the two, so one scan reflects both what exists in source and what the
build has proven.

tree-sitter is an optional extra (``pip install '<engine>[scan]'``). When it is
absent the import below fails closed: ``scan_sources`` raises ``ScanUnavailable``
and ``discovery.scan`` catches it to degrade to the facts-only inventory. The
import is guarded by ``except ImportError`` so the stdlib-only import policy
(``engine/tests/import_policy.py``) keeps passing — tree-sitter is the single
sanctioned non-stdlib dependency, and only here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

try:  # optional [scan] extra; absent in a stdlib-only install
    from tree_sitter import Language, Parser
    import tree_sitter_cpp

    _IMPORT_ERROR: Optional[str] = None
except ImportError as exc:  # pragma: no cover - exercised by the degrade path
    Language = None  # type: ignore[assignment]
    Parser = None  # type: ignore[assignment]
    tree_sitter_cpp = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


# Header suffixes are scanned too: a header-only fixture (TEST_F whose body sits
# in the .h) is still a declared test even though it is not its own TU.
SOURCE_SUFFIXES = {
    ".cc", ".cpp", ".cxx", ".c++", ".cu",
    ".hh", ".hpp", ".hxx", ".h++", ".h",
}
# gtest case-defining macros expand to a function definition.
TEST_KINDS = {"TEST", "TEST_F", "TEST_P", "TYPED_TEST", "TYPED_TEST_P"}
# Parametrized-suite instantiations expand to a call, not a function.
INSTANTIATE_KINDS = {"INSTANTIATE_TEST_SUITE_P", "INSTANTIATE_TYPED_TEST_SUITE_P"}
# Every recognized macro carries this token, so a file without it cannot declare
# a GoogleTest case and is skipped before the (relatively expensive) parse.
_TEST_MARKER = b"TEST"
# Function/block bodies never hold declaration-scope test macros, so the AST walk
# never descends into them — keeping the walk shallow on large translation units.
_SKIP_DESCEND = frozenset({"compound_statement"})


class ScanUnavailable(RuntimeError):
    """Raised when the tree-sitter ``[scan]`` extra is not installed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class DeclaredTest:
    """A GoogleTest case found in source by the AST walk (build-independent)."""

    suite: str
    name: str
    kind: str
    fixture: Optional[str]
    file: str
    line: int


def tree_sitter_available() -> bool:
    """True when the tree-sitter extra imported, so a real AST scan can run."""
    return _IMPORT_ERROR is None


def unavailable_reason() -> Optional[str]:
    """A typed reason string when the scanner is unavailable, else None."""
    return None if _IMPORT_ERROR is None else "tree_sitter_not_installed"


def scan_sources(repo_root: Path | str) -> List[DeclaredTest]:
    """Walk every C++ source under ``repo_root`` and return all declared tests.

    Raises ``ScanUnavailable`` when the tree-sitter extra is not installed;
    callers wanting graceful degradation (``discovery.scan``) catch it.
    """
    if _IMPORT_ERROR is not None:
        raise ScanUnavailable("tree_sitter_not_installed")
    return _Scanner().scan(Path(repo_root))


class _Scanner:
    def __init__(self) -> None:
        self._language = Language(tree_sitter_cpp.language())
        self._parser = Parser()
        self._parser.language = self._language

    def scan(self, root: Path) -> List[DeclaredTest]:
        found: List[DeclaredTest] = []
        for path in self._iter_sources(root):
            try:
                source = path.read_bytes()
            except OSError:
                continue
            if _TEST_MARKER not in source:
                continue
            found.extend(self._scan_file(root, path, source))
        return found

    def _iter_sources(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune hidden dirs (.git, .arbiter, .venv) and caches in place so the
            # walk never descends into engine state or VCS internals.
            dirnames[:] = [
                name for name in dirnames
                if not name.startswith(".") and name != "__pycache__"
            ]
            for name in filenames:
                if os.path.splitext(name)[1] in SOURCE_SUFFIXES:
                    yield Path(dirpath) / name

    def _scan_file(self, root: Path, path: Path, source: bytes) -> List[DeclaredTest]:
        tree = self._parser.parse(source)
        rel = self._relpath(root, path)
        out: List[DeclaredTest] = []
        for node in self._walk(tree.root_node):
            if node.type == "function_definition":
                out.extend(self._from_function(rel, source, node))
            elif node.type == "call_expression":
                out.extend(self._from_call(rel, source, node))
        return out

    def _from_function(self, rel: str, source: bytes, node) -> List[DeclaredTest]:
        declarator = self._first_child(node, "function_declarator")
        if declarator is None:
            return []
        identifier = self._first_child(declarator, "identifier")
        parameters = self._first_child(declarator, "parameter_list")
        if identifier is None or parameters is None:
            return []
        kind = self._text(source, identifier)
        if kind not in TEST_KINDS:
            return []
        args = [
            self._text(source, child)
            for child in parameters.children
            if child.type == "parameter_declaration"
        ]
        if len(args) < 2:
            return []
        suite, name = args[0], args[1]
        return [
            DeclaredTest(
                suite=suite,
                name=name,
                kind=kind,
                fixture=suite if kind != "TEST" else None,
                file=rel,
                line=int(node.start_point[0]) + 1,
            )
        ]

    def _from_call(self, rel: str, source: bytes, node) -> List[DeclaredTest]:
        identifier = self._first_child(node, "identifier")
        arguments = self._first_child(node, "argument_list")
        if identifier is None or arguments is None:
            return []
        kind = self._text(source, identifier)
        if kind not in INSTANTIATE_KINDS:
            return []
        args = [child for child in arguments.children if child.is_named]
        if len(args) < 2:
            return []
        prefix = self._text(source, args[0])
        suite = self._text(source, args[1])
        # gtest runs a parametrized instantiation under the filter
        # ``Prefix/Suite.*``; carry that whole token as the suite so the
        # TestCandidate.test form (``suite.name``) is the runnable filter.
        return [
            DeclaredTest(
                suite=f"{prefix}/{suite}",
                name="*",
                kind=kind,
                fixture=None,
                file=rel,
                line=int(node.start_point[0]) + 1,
            )
        ]

    def _first_child(self, node, node_type: str):
        for child in node.children:
            if child.type == node_type:
                return child
        return None

    def _walk(self, node):
        yield node
        for child in node.children:
            if child.type in _SKIP_DESCEND:
                continue
            yield from self._walk(child)

    def _text(self, source: bytes, node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def _relpath(self, root: Path, path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)


__all__ = [
    "DeclaredTest",
    "ScanUnavailable",
    "scan_sources",
    "tree_sitter_available",
    "unavailable_reason",
]

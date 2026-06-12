"""Independent Clang AST gold extraction helpers.

This module is intentionally import-only. It can build a small gold graph from
Clang 16 AST JSON for manual benchmark manifests, but the default smoke tests
use explicit manifest cases so they do not require Clang.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from benchmarks.retrieval.models import GoldCall, GoldFieldAccess, GoldFunction, GoldGraph, RetrievalBenchmarkError


def build_gold_graph(
    *,
    repo_root: Path,
    clang_executable: str,
    sources: Iterable[str],
    clang_args: Optional[Iterable[str]] = None,
) -> GoldGraph:
    functions: List[GoldFunction] = []
    calls: List[GoldCall] = []
    field_accesses: List[GoldFieldAccess] = []
    for source in sources:
        ast = _clang_ast_json(repo_root, clang_executable, source, list(clang_args or []))
        _walk_ast(ast, current_function=None, functions=functions, calls=calls, field_accesses=field_accesses)
    return GoldGraph(functions=functions, calls=calls, field_accesses=field_accesses)


def _clang_ast_json(repo_root: Path, clang_executable: str, source: str, clang_args: List[str]) -> Dict[str, Any]:
    command = [
        clang_executable,
        "-Xclang",
        "-ast-dump=json",
        "-fsyntax-only",
        *clang_args,
        str(repo_root / source),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise RetrievalBenchmarkError("clang_unavailable", f"failed to run Clang: {exc}") from exc
    if completed.returncode != 0:
        raise RetrievalBenchmarkError("clang_failed", completed.stderr.strip() or "Clang AST extraction failed")
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RetrievalBenchmarkError("clang_invalid_json", f"Clang AST output is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RetrievalBenchmarkError("clang_invalid_json", "Clang AST root must be an object")
    return data


def _walk_ast(
    node: Any,
    *,
    current_function: Optional[str],
    functions: List[GoldFunction],
    calls: List[GoldCall],
    field_accesses: List[GoldFieldAccess],
) -> None:
    if not isinstance(node, dict):
        return
    kind = node.get("kind")
    next_function = current_function
    if kind == "FunctionDecl" and isinstance(node.get("name"), str):
        source = _node_source(node)
        next_function = node["name"]
        functions.append(GoldFunction(name=node["name"], source=source))
    elif kind == "CallExpr" and current_function:
        callee = _referenced_name(node)
        if callee:
            calls.append(GoldCall(caller=current_function, callee=callee, source=_node_source(node)))
    elif kind == "MemberExpr" and current_function:
        field_name = _referenced_name(node) or node.get("name")
        if isinstance(field_name, str) and field_name:
            field_accesses.append(
                GoldFieldAccess(
                    accessor=current_function,
                    field_name=field_name,
                    access_kind="read",
                    source=_node_source(node),
                )
            )
    for child in node.get("inner") or []:
        _walk_ast(
            child,
            current_function=next_function,
            functions=functions,
            calls=calls,
            field_accesses=field_accesses,
        )


def _referenced_name(node: Dict[str, Any]) -> Optional[str]:
    referenced = node.get("referencedDecl")
    if isinstance(referenced, dict) and isinstance(referenced.get("name"), str):
        return referenced["name"]
    for child in node.get("inner") or []:
        if isinstance(child, dict):
            found = _referenced_name(child)
            if found:
                return found
    return None


def _node_source(node: Dict[str, Any]) -> str:
    loc = node.get("loc")
    if isinstance(loc, dict):
        file_name = loc.get("file")
        included_from = loc.get("includedFrom")
        if not file_name and isinstance(included_from, dict):
            file_name = included_from.get("file")
        line = loc.get("line")
        if isinstance(file_name, str) and isinstance(line, int):
            return f"{file_name}:{line}"
        if isinstance(file_name, str):
            return file_name
    return "<unknown-source>"

"""Compile database generation from arbiter cc journals."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm"}
SEPARATE_PATH_FLAGS = {
    "-I",
    "-F",
    "-iquote",
    "-isystem",
    "-idirafter",
    "-isysroot",
    "--sysroot",
    "-include",
    "-imacros",
    "-o",
}
JOINED_PATH_PREFIXES = ("-I", "-F", "-iquote", "-isystem", "-idirafter", "-isysroot")


@dataclass(frozen=True)
class EmitResult:
    path: Path
    entries: int
    fallback_used: bool = False


def emit(
    journals: Sequence[Path | str],
    output_path: Path | str,
    *,
    fallback: Sequence[str] | None = None,
    cwd: Path | str | None = None,
) -> EmitResult:
    output = Path(output_path)
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for record in _read_records(journals):
        entry = _compile_command(record)
        if entry is None:
            continue
        key = (entry["file"], entry.get("output", ""))
        entries[key] = entry

    if not entries and fallback:
        subprocess.run(list(fallback), cwd=os.fspath(cwd) if cwd is not None else None, check=True)
        return EmitResult(output, _count_existing(output), fallback_used=True)

    payload = [entries[key] for key in sorted(entries)]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return EmitResult(output, len(payload))


def _read_records(journals: Sequence[Path | str]) -> Iterable[Mapping[str, Any]]:
    for journal in journals:
        path = Path(journal)
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record


def _compile_command(record: Mapping[str, Any]) -> dict[str, Any] | None:
    if record.get("miss"):
        return None
    argv = record.get("argv")
    cwd = record.get("cwd")
    src = record.get("src")
    out = record.get("out", "")
    if not _string_list(argv) or not isinstance(cwd, str) or not isinstance(src, str):
        return None
    if out and not isinstance(out, str):
        return None

    cwd_path = Path(cwd)
    expanded = _expand_response_files(argv, cwd_path)
    normalized_args = _normalize_args(expanded, cwd_path)
    file_path = _normalize_path(src, cwd_path)
    entry: dict[str, Any] = {
        "arguments": normalized_args,
        "directory": str(cwd_path),
        "file": file_path,
    }
    if out:
        entry["output"] = _normalize_path(out, cwd_path)
    return entry


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _expand_response_files(argv: Sequence[str], cwd: Path) -> list[str]:
    expanded: list[str] = []
    for arg in argv:
        if arg.startswith("@") and len(arg) > 1:
            path = Path(arg[1:])
            if not path.is_absolute():
                path = cwd / path
            try:
                expanded.extend(shlex.split(path.read_text(encoding="utf-8")))
                continue
            except OSError:
                pass
        expanded.append(arg)
    return expanded


def _normalize_args(argv: Sequence[str], cwd: Path) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in SEPARATE_PATH_FLAGS and i + 1 < len(argv):
            out.append(arg)
            out.append(_normalize_path(argv[i + 1], cwd))
            i += 2
            continue
        if arg.startswith("--sysroot="):
            out.append("--sysroot=" + _normalize_path(arg.split("=", 1)[1], cwd))
            i += 1
            continue
        joined = _split_joined_path_flag(arg)
        if joined is not None:
            prefix, value = joined
            out.append(prefix + _normalize_path(value, cwd))
            i += 1
            continue
        if _is_source(arg):
            out.append(_normalize_path(arg, cwd))
        else:
            out.append(arg)
        i += 1
    return out


def _split_joined_path_flag(arg: str) -> tuple[str, str] | None:
    for prefix in JOINED_PATH_PREFIXES:
        if arg.startswith(prefix) and len(arg) > len(prefix):
            return prefix, arg[len(prefix) :]
    return None


def _is_source(arg: str) -> bool:
    return Path(arg).suffix.lower() in SOURCE_SUFFIXES


def _normalize_path(value: str, cwd: Path) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    return os.path.normpath(os.fspath(path))


def _count_existing(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return 0
    return len(data)

"""Strict RecipeBook v2 parser."""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple


# Target ids are used to build filesystem paths (run artifacts, compile
# journals), so they must be path-safe: [A-Za-z0-9._-]+ without a leading dot
# and without '..' sequences.
SAFE_TARGET_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*\Z")

ROOT_KEYS = {"vars", "profiles", "compile_db", "targets"}
TARGET_KEYS = {
    "id",
    "binary",
    "tests",
    "workdir",
    "env",
    "harness",
    "sources",
    "requires",
    "notes",
    "src_compile",
    "test_compile",
    "test_run",
}
STAGE_KEYS = {"pre", "cmd", "post", "env", "timeout_s"}
PROFILE_KEYS = {"cflags_append", "cxxflags_append", "ldflags_append", "env"}
COMPILE_DB_KEYS = {"path", "target"}


@dataclass(frozen=True)
class _Node:
    value: Any
    line: int


class RecipeError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


@dataclass(frozen=True)
class Profile:
    cflags_append: Tuple[str, ...] = ()
    cxxflags_append: Tuple[str, ...] = ()
    ldflags_append: Tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.cflags_append:
            out["cflags_append"] = list(self.cflags_append)
        if self.cxxflags_append:
            out["cxxflags_append"] = list(self.cxxflags_append)
        if self.ldflags_append:
            out["ldflags_append"] = list(self.ldflags_append)
        if self.env:
            out["env"] = dict(sorted(self.env.items()))
        return out


@dataclass(frozen=True)
class CompileDB:
    path: str
    target: Optional[str] = None

    def to_json(self) -> dict[str, Any]:
        out = {"path": self.path}
        if self.target is not None:
            out["target"] = self.target
        return out


@dataclass(frozen=True)
class Harness:
    kind: str
    options: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "options": _plain(self.options)}


@dataclass(frozen=True)
class Stage:
    cmd: Tuple[str, ...]
    pre: Tuple[Tuple[str, ...], ...] = ()
    post: Tuple[Tuple[str, ...], ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: Optional[int] = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cmd": list(self.cmd)}
        if self.pre:
            out["pre"] = [list(cmd) for cmd in self.pre]
        if self.post:
            out["post"] = [list(cmd) for cmd in self.post]
        if self.env:
            out["env"] = dict(sorted(self.env.items()))
        if self.timeout_s is not None:
            out["timeout_s"] = self.timeout_s
        return out


@dataclass(frozen=True)
class Target:
    id: str
    binary: Optional[str]
    tests: Tuple[str, ...]
    workdir: str
    env: Mapping[str, str]
    harness: Harness
    sources: Tuple[str, ...]
    requires: Tuple[str, ...]
    notes: Optional[str]
    stages: Mapping[str, Stage]

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "harness": self.harness.to_json(),
            "id": self.id,
            "stages": {name: self.stages[name].to_json() for name in sorted(self.stages)},
            "workdir": self.workdir,
        }
        if self.binary is not None:
            out["binary"] = self.binary
        if self.tests:
            out["tests"] = list(self.tests)
        if self.env:
            out["env"] = dict(sorted(self.env.items()))
        if self.sources:
            out["sources"] = list(self.sources)
        if self.requires:
            out["requires"] = list(self.requires)
        if self.notes is not None:
            out["notes"] = self.notes
        return {key: out[key] for key in sorted(out)}


@dataclass(frozen=True)
class RecipeBook:
    vars: Mapping[str, str] = field(default_factory=dict)
    profiles: Mapping[str, Profile] = field(default_factory=dict)
    compile_db: Optional[CompileDB] = None
    targets: Tuple[Target, ...] = ()

    def target(self, target_id: str) -> Target:
        for target in self.targets:
            if target.id == target_id:
                return target
        raise KeyError(target_id)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "profiles": {name: self.profiles[name].to_json() for name in sorted(self.profiles)},
            "targets": [target.to_json() for target in self.targets],
            "vars": dict(sorted(self.vars.items())),
        }
        if self.compile_db is not None:
            out["compile_db"] = self.compile_db.to_json()
        return out

    def to_json_text(self) -> str:
        return json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n"


def load(path: Path | str) -> RecipeBook:
    return parse(Path(path).read_text(encoding="utf-8"))


def parse(text: str) -> RecipeBook:
    root = _parse_yaml_subset(text)
    values = _require_mapping(root, "recipe book")
    _reject_unknown(values, ROOT_KEYS)
    return RecipeBook(
        vars=_parse_vars(values.get("vars")),
        profiles=_parse_profiles(values.get("profiles")),
        compile_db=_parse_compile_db(values.get("compile_db")),
        targets=_parse_targets(values.get("targets")),
    )


def _parse_vars(node: Optional[_Node]) -> Mapping[str, str]:
    if node is None:
        return {}
    values = _require_mapping(node, "vars")
    return {key: _string(value, f"vars.{key}") for key, value in sorted(values.items())}


def _parse_profiles(node: Optional[_Node]) -> Mapping[str, Profile]:
    if node is None:
        return {}
    values = _require_mapping(node, "profiles")
    profiles: dict[str, Profile] = {}
    for name, child in sorted(values.items()):
        body = _require_mapping(child, f"profiles.{name}")
        _reject_unknown(body, PROFILE_KEYS)
        profiles[name] = Profile(
            cflags_append=_string_list(body.get("cflags_append"), f"profiles.{name}.cflags_append"),
            cxxflags_append=_string_list(body.get("cxxflags_append"), f"profiles.{name}.cxxflags_append"),
            ldflags_append=_string_list(body.get("ldflags_append"), f"profiles.{name}.ldflags_append"),
            env=_parse_env(body.get("env"), f"profiles.{name}.env"),
        )
    return profiles


def _parse_compile_db(node: Optional[_Node]) -> Optional[CompileDB]:
    if node is None:
        return None
    values = _require_mapping(node, "compile_db")
    _reject_unknown(values, COMPILE_DB_KEYS)
    path = _required_string(values.get("path"), "compile_db.path")
    _require_relative(path, values["path"].line, "compile_db.path")
    target = _optional_string(values.get("target"), "compile_db.target")
    return CompileDB(path=path, target=target)


def _parse_targets(node: Optional[_Node]) -> Tuple[Target, ...]:
    if node is None:
        return ()
    items = _require_sequence(node, "targets")
    targets: list[Target] = []
    seen: set[str] = set()
    for item in items:
        body = _require_mapping(item, "targets entries")
        _reject_unknown(body, TARGET_KEYS)
        target_id = _required_string(body.get("id"), "target.id")
        if not SAFE_TARGET_ID.fullmatch(target_id) or ".." in target_id:
            raise RecipeError(
                body["id"].line,
                f"target id {target_id!r} must match [A-Za-z0-9._-]+ "
                "and may not start with '.' or contain '..'",
            )
        if target_id in seen:
            raise RecipeError(body["id"].line, f"duplicate target id {target_id!r}")
        seen.add(target_id)
        binary = _optional_string(body.get("binary"), "target.binary")
        if binary is not None:
            _require_relative(binary, body["binary"].line, "binary")
        workdir = _optional_string(body.get("workdir"), "target.workdir") or "."
        _require_relative(workdir, body.get("workdir", item).line, "workdir")
        stages = {
            name: _parse_stage(body[name], name)
            for name in ("src_compile", "test_compile", "test_run")
            if name in body
        }
        if not stages:
            raise RecipeError(
                item.line,
                "target must declare at least one stage: add a 'src_compile', 'test_compile', "
                "or 'test_run' mapping directly under the target (each holds pre/cmd/post argv lists)",
            )
        targets.append(
            Target(
                id=target_id,
                binary=binary,
                tests=_string_list(body.get("tests"), "target.tests"),
                workdir=workdir,
                env=_parse_env(body.get("env"), "target.env"),
                harness=_parse_harness(body.get("harness")),
                sources=_relative_string_list(body.get("sources"), "target.sources"),
                requires=_string_list(body.get("requires"), "target.requires"),
                notes=_optional_string(body.get("notes"), "target.notes"),
                stages=stages,
            )
        )
    return tuple(targets)


def _parse_harness(node: Optional[_Node]) -> Harness:
    if node is None:
        raise RecipeError(1, "target.harness is required")
    values = _require_mapping(node, "target.harness")
    kind = _required_string(values.get("kind"), "target.harness.kind")
    options = {
        key: _plain(value.value)
        for key, value in sorted(values.items())
        if key != "kind"
    }
    return Harness(kind=kind, options=options)


def _parse_stage(node: _Node, name: str) -> Stage:
    values = _require_mapping(node, name)
    _reject_unknown(values, STAGE_KEYS)
    return Stage(
        cmd=_command(values.get("cmd"), f"{name}.cmd"),
        pre=_command_list(values.get("pre"), f"{name}.pre"),
        post=_command_list(values.get("post"), f"{name}.post"),
        env=_parse_env(values.get("env"), f"{name}.env"),
        timeout_s=_optional_int(values.get("timeout_s"), f"{name}.timeout_s"),
    )


def _parse_env(node: Optional[_Node], name: str) -> Mapping[str, str]:
    if node is None:
        return {}
    values = _require_mapping(node, name)
    return {key: _string(value, f"{name}.{key}") for key, value in sorted(values.items())}


def _command(node: Optional[_Node], name: str) -> Tuple[str, ...]:
    if node is None:
        raise RecipeError(1, f"{name} is required")
    if isinstance(node.value, str):
        # A scalar string is exec'd directly as ONE program name (no shell), so a string
        # carrying spaces — arguments, 'cd', '&&', pipes — can never run: the whole string
        # is taken as a single executable. Reject it here with the correct shape instead of
        # letting it fail at build time as an opaque "command not found". Char-match only.
        stripped = node.value.strip()
        if " " in stripped:
            raise RecipeError(
                node.line,
                f"{name} runs as ONE command by direct exec, never through a shell, so a "
                f"string with spaces cannot work — the entire string is taken as a single "
                f"program name. Write the command as an inline argv list instead, one token "
                f"per item: e.g. cmd: [cmake, --build, build, --target, NAME], or "
                f"cmd: [./build/tests]. There is no shell, so 'cd', '&&', ';', '|' and "
                f"redirection are unavailable: put each command as its own item under 'pre' "
                f"(a list of argv lists) and use flags like cmake's -S/-B in place of 'cd'.",
            )
        # A command's first (here: only) token beginning with '-' is a flag, not a program —
        # the tell-tale of an argv list mis-split across separate list items
        # (pre: [cmake, -S, ., -B, build]). Each pre/post item must be ONE complete command
        # written as an inline list; catch the split here, not as an opaque build failure.
        if stripped.startswith("-"):
            raise RecipeError(
                node.line,
                f"{name} item {node.value!r} begins with '-', so it is a flag, not a program. "
                f"A command's first token must be the executable — you have split one command "
                f"across separate list items. Write the WHOLE command as a single inline list, "
                f"e.g. pre: [[cmake, -S, ., -B, build, -DCMAKE_C_COMPILER=...]] (pre/post are "
                f"lists OF argv lists: one list item per command, and each item is the complete "
                f"[program, arg, arg] inline list — never one argument per item).",
            )
        return (node.value,)
    if isinstance(node.value, list) and all(isinstance(item, str) for item in node.value):
        if not node.value:
            raise RecipeError(node.line, f"{name} must not be empty")
        return tuple(node.value)
    raise RecipeError(node.line, f"{name} must be a string or inline string list")


def _command_list(node: Optional[_Node], name: str) -> Tuple[Tuple[str, ...], ...]:
    if node is None:
        return ()
    if not isinstance(node.value, list):
        raise RecipeError(node.line, f"{name} must be a list")
    return tuple(_command(item if isinstance(item, _Node) else _Node(item, node.line), name) for item in node.value)


def _parse_yaml_subset(text: str) -> _Node:
    lines = _logical_lines(text)
    if not lines:
        return _Node({}, 1)
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise RecipeError(lines[index][2], "unexpected trailing content")
    return value


def _logical_lines(text: str) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise RecipeError(line_no, "tabs are not allowed")
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent % 2:
            raise RecipeError(line_no, "indentation must use two-space steps")
        out.append((indent, stripped[indent:], line_no))
    return out


def _parse_block(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[_Node, int]:
    if lines[index][0] != indent:
        raise RecipeError(lines[index][2], "indentation jumps more than one mapping level")
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(
    lines: list[tuple[int, str, int]],
    index: int,
    indent: int,
    initial: Optional[dict[str, _Node]] = None,
) -> tuple[_Node, int]:
    result: dict[str, _Node] = {} if initial is None else dict(initial)
    start_line = lines[index][2]
    while index < len(lines):
        current_indent, body, line = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise RecipeError(line, "indentation jumps more than one mapping level")
        if body.startswith("- "):
            break
        key, value_text = _split_key_value(body, line)
        if key in result:
            raise RecipeError(line, f"duplicate key {key!r}")
        if value_text == "":
            if index + 1 >= len(lines) or lines[index + 1][0] <= indent:
                raise RecipeError(line, f"{key} requires an indented value")
            child, index = _parse_block(lines, index + 1, indent + 2)
            result[key] = _Node(child.value, line)
        else:
            result[key] = _Node(_parse_value(value_text, line), line)
            index += 1
    return _Node(result, start_line), index


def _parse_sequence(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[_Node, int]:
    items: list[_Node] = []
    start_line = lines[index][2]
    while index < len(lines):
        current_indent, body, line = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise RecipeError(line, "indentation jumps more than one mapping level")
        if not body.startswith("- "):
            break
        item_text = body[2:].strip()
        if item_text == "":
            if index + 1 >= len(lines) or lines[index + 1][0] <= indent:
                raise RecipeError(line, "sequence item requires an indented value")
            child, index = _parse_block(lines, index + 1, indent + 2)
            items.append(_Node(child.value, line))
            continue
        if ":" in item_text and not item_text.startswith(('"', "'", "[")):
            key, value_text = _split_key_value(item_text, line)
            initial = {key: _Node(_parse_value(value_text, line), line)}
            index += 1
            if index < len(lines) and lines[index][0] == indent + 2 and not lines[index][1].startswith("- "):
                child, index = _parse_mapping(lines, index, indent + 2, initial)
                items.append(_Node(child.value, line))
            else:
                items.append(_Node(initial, line))
            continue
        items.append(_Node(_parse_value(item_text, line), line))
        index += 1
    return _Node(items, start_line), index


def _split_key_value(body: str, line: int) -> tuple[str, str]:
    if ":" not in body:
        raise RecipeError(line, "expected mapping entry")
    key, value_text = body.split(":", 1)
    key = key.strip()
    if not _valid_key(key):
        raise RecipeError(line, f"invalid key {key!r}")
    return key, value_text.strip()


def _strip_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
            continue
        if char == "#" and not quote:
            return line[:index]
    return line


def _parse_value(text: str, line: int) -> Any:
    if text == "{}":
        return {}
    if text.startswith("["):
        if not text.endswith("]"):
            raise RecipeError(line, "inline lists must close on the same line")
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item.strip(), line) for item in _split_inline_list(inner, line)]
    return _parse_scalar(text, line)


def _parse_scalar(text: str, line: int) -> Any:
    if not text:
        raise RecipeError(line, "empty scalar is not allowed")
    if text.startswith(("&", "*")):
        raise RecipeError(line, "anchors are not supported")
    if text.startswith(("{", "}")):
        raise RecipeError(line, "inline maps are not supported")
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if text.startswith(('"', "'")):
        try:
            value = ast.literal_eval(text)
        except (SyntaxError, ValueError) as exc:
            raise RecipeError(line, f"invalid quoted string: {exc}") from exc
        if not isinstance(value, str):
            raise RecipeError(line, "quoted value must be a string")
        return value
    if _looks_int(text):
        return int(text)
    return text


def _split_inline_list(text: str, line: int) -> list[str]:
    parts: list[str] = []
    start = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
            continue
        if char == "," and not quote:
            parts.append(text[start:index])
            start = index + 1
    if quote:
        raise RecipeError(line, "unterminated quoted string in inline list")
    parts.append(text[start:])
    return parts


def _require_mapping(node: _Node, name: str) -> dict[str, _Node]:
    if not isinstance(node.value, dict):
        raise RecipeError(node.line, f"{name} must be a mapping")
    return node.value


def _require_sequence(node: _Node, name: str) -> list[_Node]:
    if not isinstance(node.value, list):
        raise RecipeError(
            node.line,
            f"{name} must be a sequence — write each entry as a '- ' list item on its own line, "
            f"not a mapping",
        )
    return [item if isinstance(item, _Node) else _Node(item, node.line) for item in node.value]


def _reject_unknown(values: Mapping[str, _Node], allowed: set[str]) -> None:
    for key, node in values.items():
        if key not in allowed:
            allowed_list = ", ".join(sorted(allowed))
            raise RecipeError(
                node.line,
                f"unknown key {key!r}; at this level the only allowed keys are: {allowed_list}",
            )


def _required_string(node: Optional[_Node], name: str) -> str:
    if node is None:
        raise RecipeError(1, f"{name} is required")
    return _string(node, name)


def _optional_string(node: Optional[_Node], name: str) -> Optional[str]:
    if node is None:
        return None
    return _string(node, name)


def _string(node: _Node, name: str) -> str:
    if not isinstance(node.value, str):
        raise RecipeError(node.line, f"{name} must be a string")
    return node.value


def _string_list(node: Optional[_Node], name: str) -> Tuple[str, ...]:
    if node is None:
        return ()
    if not isinstance(node.value, list) or not all(isinstance(item, str) for item in node.value):
        raise RecipeError(node.line, f"{name} must be an inline string list")
    return tuple(node.value)


def _relative_string_list(node: Optional[_Node], name: str) -> Tuple[str, ...]:
    values = _string_list(node, name)
    if node is not None:
        for value in values:
            _require_relative(value, node.line, name)
    return values


def _optional_int(node: Optional[_Node], name: str) -> Optional[int]:
    if node is None:
        return None
    if not isinstance(node.value, int) or isinstance(node.value, bool):
        raise RecipeError(node.line, f"{name} must be an integer")
    return node.value


def _require_relative(value: str, line: int, name: str) -> None:
    if os.path.isabs(value):
        raise RecipeError(line, f"{name} must be relative")


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(inner) for key, inner in sorted(value.items())}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _valid_key(key: str) -> bool:
    if not key or not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in key)


def _looks_int(text: str) -> bool:
    number = text[1:] if text.startswith("-") else text
    return bool(number) and all(char.isdigit() for char in number)

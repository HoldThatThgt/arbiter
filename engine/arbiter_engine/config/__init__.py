"""Strict stdlib-only parser for .arbiter/config.yml."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class IndexOnBuildConfig:
    pool: Optional[int] = None
    key_flags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FactsConfig:
    extractor: Optional[str] = None
    incremental: Optional[bool] = None
    index_on_build: IndexOnBuildConfig = field(default_factory=IndexOnBuildConfig)


@dataclass(frozen=True)
class MatchConfig:
    goal_memo: bool = False


@dataclass(frozen=True)
class Config:
    facts: FactsConfig = field(default_factory=FactsConfig)
    runs: Mapping[str, Any] = field(default_factory=dict)
    match: MatchConfig = field(default_factory=MatchConfig)
    engine: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Node:
    value: Any
    line: int


class ConfigError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


def load_config(path: Path) -> Config:
    return parse_config(Path(path).read_text(encoding="utf-8"))


def parse_config(text: str) -> Config:
    root = _parse_mapping(text)
    _reject_unknown(root, {"facts", "runs", "match", "engine"})
    return Config(
        facts=_parse_facts(root.get("facts")),
        runs=_parse_empty_section(root.get("runs"), "runs"),
        match=_parse_match(root.get("match")),
        engine=_parse_empty_section(root.get("engine"), "engine"),
    )


def _parse_mapping(text: str) -> dict[str, _Node]:
    root: dict[str, _Node] = {}
    stack: list[tuple[int, dict[str, _Node]]] = [(-2, root)]

    for line_no, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise ConfigError(line_no, "tabs are not allowed")
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue

        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent % 2:
            raise ConfigError(line_no, "indentation must use two-space steps")
        body = stripped[indent:]
        if body.startswith("-"):
            raise ConfigError(line_no, "expected mapping entry; block sequences are not supported")
        if ":" not in body:
            raise ConfigError(line_no, "expected mapping entry")

        key, value_text = body.split(":", 1)
        key = key.strip()
        if not _valid_key(key):
            raise ConfigError(line_no, f"invalid key {key!r}")

        while indent <= stack[-1][0]:
            stack.pop()
        if indent > stack[-1][0] + 2:
            raise ConfigError(line_no, "indentation jumps more than one mapping level")

        parent = stack[-1][1]
        if key in parent:
            raise ConfigError(line_no, f"duplicate key {key!r}")

        value_text = value_text.strip()
        if value_text == "":
            child: dict[str, _Node] = {}
            parent[key] = _Node(child, line_no)
            stack.append((indent, child))
        else:
            parent[key] = _Node(_parse_value(value_text, line_no), line_no)

    return root


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
    if text.startswith("["):
        if not text.endswith("]"):
            raise ConfigError(line, "inline lists must close on the same line")
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item.strip(), line) for item in _split_inline_list(inner, line)]
    return _parse_scalar(text, line)


def _parse_scalar(text: str, line: int) -> Any:
    if not text:
        raise ConfigError(line, "empty scalar is not allowed")
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if text.startswith(('"', "'")):
        try:
            value = ast.literal_eval(text)
        except (SyntaxError, ValueError) as exc:
            raise ConfigError(line, f"invalid quoted string: {exc}") from exc
        if not isinstance(value, str):
            raise ConfigError(line, "quoted value must be a string")
        return value
    if text.startswith(("&", "*", "{", "}")):
        raise ConfigError(line, "unsupported YAML feature")
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
        raise ConfigError(line, "unterminated quoted string in inline list")
    parts.append(text[start:])
    return parts


def _parse_facts(node: Optional[_Node]) -> FactsConfig:
    if node is None:
        return FactsConfig()
    values = _require_mapping(node, "facts")
    _reject_unknown(values, {"extractor", "incremental", "index_on_build"})
    return FactsConfig(
        extractor=_optional_string(values.get("extractor"), "facts.extractor"),
        incremental=_optional_bool(values.get("incremental"), "facts.incremental"),
        index_on_build=_parse_index_on_build(values.get("index_on_build")),
    )


def _parse_index_on_build(node: Optional[_Node]) -> IndexOnBuildConfig:
    if node is None:
        return IndexOnBuildConfig()
    values = _require_mapping(node, "facts.index_on_build")
    _reject_unknown(values, {"pool", "key_flags"})
    return IndexOnBuildConfig(
        pool=_optional_int(values.get("pool"), "facts.index_on_build.pool"),
        key_flags=_optional_string_list(
            values.get("key_flags"), "facts.index_on_build.key_flags"
        ),
    )


def _parse_match(node: Optional[_Node]) -> MatchConfig:
    if node is None:
        return MatchConfig()
    values = _require_mapping(node, "match")
    _reject_unknown(values, {"goal_memo"})
    goal_memo = _optional_bool(values.get("goal_memo"), "match.goal_memo")
    return MatchConfig(goal_memo=False if goal_memo is None else goal_memo)


def _parse_empty_section(node: Optional[_Node], name: str) -> Mapping[str, Any]:
    if node is None:
        return {}
    values = _require_mapping(node, name)
    _reject_unknown(values, set())
    return {}


def _require_mapping(node: _Node, name: str) -> dict[str, _Node]:
    if not isinstance(node.value, dict):
        raise ConfigError(node.line, f"{name} must be a mapping")
    return node.value


def _reject_unknown(values: Mapping[str, _Node], allowed: set[str]) -> None:
    for key, node in values.items():
        if key not in allowed:
            raise ConfigError(node.line, f"unknown key {key!r}")


def _optional_string(node: Optional[_Node], name: str) -> Optional[str]:
    if node is None:
        return None
    if not isinstance(node.value, str):
        raise ConfigError(node.line, f"{name} must be a string")
    return node.value


def _optional_bool(node: Optional[_Node], name: str) -> Optional[bool]:
    if node is None:
        return None
    if not isinstance(node.value, bool):
        raise ConfigError(node.line, f"{name} must be a boolean")
    return node.value


def _optional_int(node: Optional[_Node], name: str) -> Optional[int]:
    if node is None:
        return None
    if not isinstance(node.value, int) or isinstance(node.value, bool):
        raise ConfigError(node.line, f"{name} must be an integer")
    return node.value


def _optional_string_list(node: Optional[_Node], name: str) -> Tuple[str, ...]:
    if node is None:
        return ()
    if not isinstance(node.value, list):
        raise ConfigError(node.line, f"{name} must be an inline list")
    for item in node.value:
        if not isinstance(item, str):
            raise ConfigError(node.line, f"{name} entries must be strings")
    return tuple(node.value)


def _valid_key(key: str) -> bool:
    if not key or not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in key)


def _looks_int(text: str) -> bool:
    number = text[1:] if text.startswith("-") else text
    return bool(number) and all(char.isdigit() for char in number)

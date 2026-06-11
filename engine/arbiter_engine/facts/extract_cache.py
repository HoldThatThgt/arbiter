"""Semantic extract-cache keys for facts extraction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


_DROP_VALUE_FLAGS = frozenset({"-o", "-MF", "-MT", "-MQ"})


@dataclass(frozen=True)
class ExtractUnit:
    source: str
    tu_content: str | bytes
    include_closure: Mapping[str, str | bytes]
    flags: Sequence[str]
    toolchain_id: str

    def key(self, *, key_flags: Iterable[str] = ()) -> str:
        return key_for_unit(self, key_flags=key_flags)


def key_for_unit(unit: ExtractUnit, *, key_flags: Iterable[str] = ()) -> str:
    payload = {
        "tu": _content_sha(unit.tu_content),
        "include_closure": _include_closure_sha(unit.include_closure),
        "flags": list(clean_semantic_flags(unit.flags, key_flags=key_flags)),
        "toolchain_id": unit.toolchain_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "extract:" + hashlib.sha256(raw).hexdigest()


def changed_sources(
    before: Iterable[ExtractUnit],
    after: Iterable[ExtractUnit],
    *,
    key_flags: Iterable[str] = (),
) -> tuple[str, ...]:
    before_keys = {unit.source: key_for_unit(unit, key_flags=key_flags) for unit in before}
    changed = []
    for unit in after:
        if before_keys.get(unit.source) != key_for_unit(unit, key_flags=key_flags):
            changed.append(unit.source)
    return tuple(sorted(changed))


def clean_semantic_flags(flags: Iterable[str], *, key_flags: Iterable[str] = ()) -> tuple[str, ...]:
    key_flag_set = frozenset(key_flags)
    cleaned = []
    iterator = iter(flags)
    for flag in iterator:
        if flag in _DROP_VALUE_FLAGS:
            next(iterator, None)
            continue
        if _is_codegen_flag(flag) and flag not in key_flag_set:
            continue
        cleaned.append(flag)
    return tuple(cleaned)


def _is_codegen_flag(flag: str) -> bool:
    # NOTE: -fsanitize=* is deliberately NOT treated as codegen-only.
    # Sanitizers inject preprocessor state (__SANITIZE_*, __has_feature(*_sanitizer)),
    # so sanitizer flags always participate in the semantic key (ADR-0005).
    if flag in {"--coverage", "-coverage", "-c", "-S", "-E"}:
        return True
    if flag == "-O" or flag.startswith("-O"):
        return True
    if flag == "-g" or flag.startswith("-g"):
        return True
    if flag.startswith("-fprofile-"):
        return True
    return False


def _include_closure_sha(include_closure: Mapping[str, str | bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(include_closure):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_content_sha(include_closure[path]).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _content_sha(content: str | bytes) -> str:
    if isinstance(content, str):
        raw = content.encode("utf-8")
    else:
        raw = bytes(content)
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "ExtractUnit",
    "changed_sources",
    "clean_semantic_flags",
    "key_for_unit",
]

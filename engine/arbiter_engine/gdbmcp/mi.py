from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .errors import ToolError


@dataclass
class MIRecord:
    token: Optional[int]
    kind: str
    cls: str
    results: Dict[str, Any]
    text: Optional[str] = None
    raw: str = ""

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "kind": self.kind,
            "class": self.cls,
            "results": self.results,
        }
        if self.token is not None:
            out["token"] = self.token
        if self.text is not None:
            out["text"] = self.text
        return out


def quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f"\"{escaped}\""


def parse_line(line: str) -> Optional[MIRecord]:
    line = line.rstrip("\r\n")
    if not line or line == "(gdb)":
        return None
    token_text = ""
    idx = 0
    while idx < len(line) and line[idx].isdigit():
        token_text += line[idx]
        idx += 1
    token = int(token_text) if token_text else None
    if idx >= len(line):
        return MIRecord(token, "console", "", {}, raw=line)
    marker = line[idx]
    body = line[idx + 1 :]
    if marker in {"~", "@", "&"}:
        return MIRecord(token, _stream_kind(marker), "stream", {}, text=_parse_c_string(body), raw=line)
    if marker in {"^", "*", "+", "="}:
        cls, rest = _split_class(body)
        results = _parse_results(rest[1:] if rest.startswith(",") else rest)
        return MIRecord(token, _record_kind(marker), cls, results, raw=line)
    return MIRecord(token, "console", marker, {}, text=body, raw=line)


def _stream_kind(marker: str) -> str:
    return {"~": "console", "@": "target", "&": "log"}[marker]


def _record_kind(marker: str) -> str:
    return {"^": "result", "*": "exec", "+": "status", "=": "notify"}[marker]


def _split_class(body: str) -> Tuple[str, str]:
    for idx, char in enumerate(body):
        if char == ",":
            return body[:idx], body[idx:]
    return body, ""


def _parse_c_string(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == "\"" and value[-1] == "\"":
        try:
            return codecs.decode(value[1:-1], "unicode_escape")
        except Exception:
            return value[1:-1]
    return value


def _parse_results(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    parser = _Parser(text)
    return parser.parse_result_list()


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def parse_result_list(self, terminator: Optional[str] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        while self.pos < len(self.text):
            self._skip_ws()
            if terminator and self._peek() == terminator:
                self.pos += 1
                break
            key = self._parse_identifier()
            self._expect("=")
            value = self._parse_value()
            if key in out:
                existing = out[key]
                if not isinstance(existing, list):
                    out[key] = [existing]
                out[key].append(value)
            else:
                out[key] = value
            self._skip_ws()
            if self._peek() == ",":
                self.pos += 1
                continue
            if terminator and self._peek() == terminator:
                self.pos += 1
                break
            if self.pos >= len(self.text):
                break
        return out

    def _parse_value(self) -> Any:
        self._skip_ws()
        char = self._peek()
        if char == "\"":
            return self._parse_string()
        if char == "{":
            self.pos += 1
            return self.parse_result_list("}")
        if char == "[":
            return self._parse_list()
        return self._parse_bare()

    def _parse_list(self) -> List[Any]:
        self._expect("[")
        values: List[Any] = []
        while self.pos < len(self.text):
            self._skip_ws()
            if self._peek() == "]":
                self.pos += 1
                break
            start = self.pos
            ident = self._parse_identifier(allow_empty=True)
            self._skip_ws()
            if ident and self._peek() == "=":
                self.pos += 1
                values.append({ident: self._parse_value()})
            else:
                self.pos = start
                values.append(self._parse_value())
            self._skip_ws()
            if self._peek() == ",":
                self.pos += 1
        return values

    def _parse_identifier(self, allow_empty: bool = False) -> str:
        start = self.pos
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char.isalnum() or char in {"_", "-", "."}:
                self.pos += 1
            else:
                break
        if self.pos == start and not allow_empty:
            raise ToolError("mi_parse_error", "expected MI identifier", {"text": self.text, "offset": self.pos})
        return self.text[start : self.pos]

    def _parse_string(self) -> str:
        self._expect("\"")
        chunks: List[str] = []
        escaped = False
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if escaped:
                chunks.append("\\" + char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "\"":
                break
            else:
                chunks.append(char)
        try:
            return codecs.decode("".join(chunks), "unicode_escape")
        except Exception:
            return "".join(chunks).replace("\\n", "\n")

    def _parse_bare(self) -> str:
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in {",", "]", "}"}:
            self.pos += 1
        return self.text[start : self.pos].strip()

    def _skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _peek(self) -> str:
        if self.pos >= len(self.text):
            return ""
        return self.text[self.pos]

    def _expect(self, expected: str) -> None:
        if self._peek() != expected:
            raise ToolError(
                "mi_parse_error",
                f"expected {expected!r}",
                {"text": self.text, "offset": self.pos},
            )
        self.pos += 1


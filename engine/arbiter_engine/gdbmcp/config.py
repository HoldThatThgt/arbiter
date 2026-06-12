from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import ToolError


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    root: Path
    gdb_path: str
    allow_outside_root: bool = False
    allow_attach: bool = False
    allow_remote: bool = False
    allow_dangerous_commands: bool = False
    audit: bool = True
    max_sessions: int = 8
    event_limit: int = 200
    stream_limit: int = 12000

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @classmethod
    def from_env(cls, root: Optional[str] = None, gdb_path: Optional[str] = None) -> "Config":
        root_path = Path(root or os.environ.get("GDB_MCP_ROOT") or os.getcwd()).expanduser()
        max_sessions = int(os.environ.get("GDB_MCP_MAX_SESSIONS", "8"))
        return cls(
            root=root_path.resolve(),
            gdb_path=gdb_path or os.environ.get("GDB_MCP_GDB") or "gdb",
            allow_outside_root=_truthy(os.environ.get("GDB_MCP_ALLOW_OUTSIDE_ROOT")),
            allow_attach=_truthy(os.environ.get("GDB_MCP_ALLOW_ATTACH")),
            allow_remote=_truthy(os.environ.get("GDB_MCP_ALLOW_REMOTE")),
            allow_dangerous_commands=_truthy(os.environ.get("GDB_MCP_ALLOW_DANGEROUS_COMMANDS")),
            audit=not _truthy(os.environ.get("GDB_MCP_NO_AUDIT")),
            max_sessions=max_sessions,
        )

    def gdb_executable(self) -> str:
        found = shutil.which(self.gdb_path)
        if found:
            return found
        path = Path(self.gdb_path).expanduser()
        if path.exists():
            return str(path)
        raise ToolError("gdb_not_found", f"GDB executable not found: {self.gdb_path}")

    def resolve_existing_file(self, value: str, base: Optional[Path] = None, field: str = "path") -> Path:
        path = self.resolve_path(value, base=base)
        if not path.exists() or not path.is_file():
            raise ToolError("path_not_found", f"{field} does not exist or is not a file", {field: str(path)})
        return path

    def resolve_existing_dir(self, value: str, base: Optional[Path] = None, field: str = "cwd") -> Path:
        path = self.resolve_path(value, base=base)
        if not path.exists() or not path.is_dir():
            raise ToolError("path_not_found", f"{field} does not exist or is not a directory", {field: str(path)})
        return path

    def resolve_path(self, value: str, base: Optional[Path] = None) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raw = (base or self.root) / raw
        resolved = raw.resolve(strict=False)
        if not self.allow_outside_root and not _is_relative_to(resolved, self.root):
            raise ToolError(
                "path_outside_root",
                "path is outside the configured GDB MCP root",
                {"path": str(resolved), "root": str(self.root)},
            )
        return resolved

    def relative(self, path: Optional[Path]) -> Optional[str]:
        if path is None:
            return None
        try:
            return str(path.resolve(strict=False).relative_to(self.root))
        except ValueError:
            return str(path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

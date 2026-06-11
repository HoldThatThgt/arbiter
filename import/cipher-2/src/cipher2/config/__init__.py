"""Repository-local config loading for cipher-2."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from cipher2.common import JSONValue
from cipher2.tools.log import LogError, LogEvent, open_log


SCHEMA_VERSION = 1
CONFIG_FILENAME = "config.yml"
CONFIG_TMP_FILENAME = "config.yml.tmp"
INCREMENTAL_DEFAULTS = {
    "temporary_enabled": True,
    "poll_interval_ms": 500,
    "debounce_ms": 100,
    "worker_count": 1,
    "overlay_ttl_seconds": 600,
    "max_dirty_files": 500,
}
INCREMENTAL_RANGES = {
    "poll_interval_ms": (100, 5000),
    "debounce_ms": (50, 1000),
    "worker_count": (1, 8),
    "overlay_ttl_seconds": (10, 3600),
    "max_dirty_files": (1, 10000),
}
EXTRACTOR_WORKER_COUNT_RANGE = (1, 32)


class ConfigError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: Optional[Path] = None,
        details: Optional[Dict[str, JSONValue]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = str(path) if path is not None else None
        self.details = dict(details or {})


@dataclass(frozen=True)
class CipherConfig:
    schema_version: int
    target_repo: Path
    config_path: Path
    cipher_dir: Path
    storage_snapshot_dir: Path
    log_dir: Path
    compile_database_path: Optional[Path]
    clang_executable: Optional[str]
    libclang_library_path: Optional[Path]
    gcc_executable: Optional[str]
    clang_args: List[str]
    extractor_worker_count: int
    incremental_temporary_enabled: bool
    incremental_poll_interval_ms: int
    incremental_debounce_ms: int
    incremental_worker_count: int
    incremental_overlay_ttl_seconds: int
    incremental_max_dirty_files: int

    def to_mapping(self) -> Dict[str, JSONValue]:
        return {
            "schema_version": self.schema_version,
            "paths": {
                "compile_database": _serialize_compile_database(self.target_repo, self.compile_database_path),
            },
            "extractor": {
                "worker_count": self.extractor_worker_count,
                "code": {
                    "clang_executable": _serialize_tool_executable(self.target_repo, self.clang_executable),
                    "libclang_library": _serialize_compile_database(self.target_repo, self.libclang_library_path),
                    "gcc_executable": _serialize_tool_executable(self.target_repo, self.gcc_executable),
                    "clang_args": list(self.clang_args),
                },
            },
            "incremental": {
                "temporary_enabled": self.incremental_temporary_enabled,
                "poll_interval_ms": self.incremental_poll_interval_ms,
                "debounce_ms": self.incremental_debounce_ms,
                "worker_count": self.incremental_worker_count,
                "overlay_ttl_seconds": self.incremental_overlay_ttl_seconds,
                "max_dirty_files": self.incremental_max_dirty_files,
            },
        }


def load_config(
    target_repo: Path,
    *,
    overrides: Optional[Dict[str, Any]] = None,
    observe: bool = True,
) -> CipherConfig:
    target = Path(target_repo)
    started = time.perf_counter()
    config_exists = False
    scope = "none"
    has_compile_database = False
    clang_executable_scope = "none"
    gcc_executable_scope = "none"
    libclang_library_scope = "none"
    clang_arg_count = 0
    extractor_worker_count = _auto_extractor_worker_count()
    incremental_enabled = bool(INCREMENTAL_DEFAULTS["temporary_enabled"])
    incremental_worker_count = int(INCREMENTAL_DEFAULTS["worker_count"])
    incremental_poll_interval_ms = int(INCREMENTAL_DEFAULTS["poll_interval_ms"])
    legacy_section_count = 0
    try:
        target_resolved, cipher_resolved = _validate_cipher_dir(target)
        config_path = _safe_cipher_path_with_resolved(target, target_resolved, cipher_resolved, CONFIG_FILENAME)
        config_exists = config_path.exists()
        mapping = _read_config_file(config_path) if config_exists else _default_mapping()
        if overrides is not None:
            mapping = _apply_overrides(mapping, overrides)
        raw_extractor = mapping.get("extractor", {}) if isinstance(mapping, dict) else {}
        raw_code = raw_extractor.get("code", {}) if isinstance(raw_extractor, dict) else {}
        libclang_library_scope = _compile_database_scope(
            raw_code.get("libclang_library") if isinstance(raw_code, dict) else None
        )
        config, scope, legacy_section_count = _config_from_mapping(target, mapping, cipher_resolved)
        has_compile_database = config.compile_database_path is not None
        clang_executable_scope = _tool_scope(config.clang_executable)
        gcc_executable_scope = _tool_scope(config.gcc_executable)
        clang_arg_count = len(config.clang_args)
        extractor_worker_count = config.extractor_worker_count
        incremental_enabled = config.incremental_temporary_enabled
        incremental_worker_count = config.incremental_worker_count
        incremental_poll_interval_ms = config.incremental_poll_interval_ms
    except ConfigError as exc:
        if observe:
            _emit_config_event(
                target,
                event_name="config.error",
                operation="load_config",
                outcome="failed",
                status="error",
                started=started,
                config_exists=config_exists,
                has_compile_database=has_compile_database,
                compile_database_scope=scope,
                clang_executable_scope=clang_executable_scope,
                libclang_library_scope=libclang_library_scope,
                gcc_executable_scope=gcc_executable_scope,
                clang_arg_count=clang_arg_count,
                extractor_worker_count=extractor_worker_count,
                incremental_enabled=incremental_enabled,
                incremental_worker_count=incremental_worker_count,
                incremental_poll_interval_ms=incremental_poll_interval_ms,
                error_code=exc.code,
            )
        raise

    if observe:
        _emit_config_event(
            target,
            event_name="config.load",
            operation="load_config",
            outcome="loaded_with_legacy_ignored" if legacy_section_count else ("loaded" if config_exists else "default"),
            status="warning" if legacy_section_count else "ok",
            started=started,
            config_exists=config_exists,
            has_compile_database=has_compile_database,
            compile_database_scope=scope,
            clang_executable_scope=clang_executable_scope,
            libclang_library_scope=libclang_library_scope,
            gcc_executable_scope=gcc_executable_scope,
            clang_arg_count=clang_arg_count,
            extractor_worker_count=extractor_worker_count,
            incremental_enabled=incremental_enabled,
            incremental_worker_count=incremental_worker_count,
            incremental_poll_interval_ms=incremental_poll_interval_ms,
            legacy_section_count=legacy_section_count,
        )
    return config


def write_default_config(
    target_repo: Path,
    *,
    compile_database: Optional[Union[str, Path]] = None,
    clang_executable: Optional[Union[str, Path]] = None,
    gcc_executable: Optional[Union[str, Path]] = None,
    libclang_library: Optional[Union[str, Path]] = None,
    clang_args: Optional[List[str]] = None,
    extractor_worker_count: Optional[int] = None,
    incremental: Optional[Dict[str, Any]] = None,
    observe: bool = True,
) -> CipherConfig:
    target = Path(target_repo)
    started = time.perf_counter()
    scope = _compile_database_scope(compile_database)
    clang_executable_scope = _tool_input_scope(clang_executable)
    gcc_executable_scope = _tool_input_scope(gcc_executable)
    libclang_library_scope = _compile_database_scope(libclang_library)
    clang_arg_count = len(clang_args or [])
    normalized_extractor_worker_count = _normalize_extractor_worker_count(extractor_worker_count)
    normalized_incremental = _normalize_incremental(incremental)
    try:
        compile_database_path = (
            normalize_compile_database_path(target, compile_database)
            if compile_database is not None
            else None
        )
        _target_resolved, cipher_resolved = _validate_cipher_dir(target)
        config = _make_config(
            target,
            compile_database_path,
            _normalize_tool_executable(
                target,
                clang_executable,
                cipher_resolved,
                config_key="extractor.code.clang_executable",
                unavailable_code="clang_unavailable",
                tool_name="clang",
            ),
            _normalize_libclang_library_path(target, libclang_library, cipher_resolved),
            _normalize_tool_executable(
                target,
                gcc_executable,
                cipher_resolved,
                config_key="extractor.code.gcc_executable",
                unavailable_code="gcc_unavailable",
                tool_name="gcc",
            ),
            _normalize_clang_args([] if clang_args is None else clang_args),
            normalized_extractor_worker_count,
            normalized_incremental,
        )
        mapping = config.to_mapping()
        mapping["extractor"]["worker_count"] = extractor_worker_count
        if isinstance(mapping.get("extractor"), dict) and isinstance(mapping["extractor"].get("code"), dict):
            mapping["extractor"]["code"]["libclang_library"] = libclang_library
        config_path = safe_cipher_path(target, CONFIG_FILENAME)
        tmp_path = safe_cipher_path(target, CONFIG_TMP_FILENAME)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(_dump_mapping(mapping), encoding="utf-8")
        os.replace(tmp_path, config_path)
    except ConfigError as exc:
        if observe:
            _emit_config_event(
                target,
                event_name="config.error",
                operation="write_default_config",
                outcome="failed",
                status="error",
                started=started,
                config_exists=(target / ".cipher" / CONFIG_FILENAME).exists(),
                has_compile_database=compile_database is not None,
                compile_database_scope=scope,
                clang_executable_scope=clang_executable_scope,
                libclang_library_scope=libclang_library_scope,
                gcc_executable_scope=gcc_executable_scope,
                clang_arg_count=clang_arg_count,
                extractor_worker_count=normalized_extractor_worker_count,
                incremental_enabled=bool(normalized_incremental["temporary_enabled"]),
                incremental_worker_count=int(normalized_incremental["worker_count"]),
                incremental_poll_interval_ms=int(normalized_incremental["poll_interval_ms"]),
                error_code=exc.code,
            )
        raise
    except OSError as exc:
        error = ConfigError("config_write_failed", "failed to write config file", path=target / ".cipher" / CONFIG_FILENAME)
        if observe:
            _emit_config_event(
                target,
                event_name="config.error",
                operation="write_default_config",
                outcome="failed",
                status="error",
                started=started,
                config_exists=(target / ".cipher" / CONFIG_FILENAME).exists(),
                has_compile_database=compile_database is not None,
                compile_database_scope=scope,
                clang_executable_scope=clang_executable_scope,
                libclang_library_scope=libclang_library_scope,
                gcc_executable_scope=gcc_executable_scope,
                clang_arg_count=clang_arg_count,
                extractor_worker_count=normalized_extractor_worker_count,
                incremental_enabled=bool(normalized_incremental["temporary_enabled"]),
                incremental_worker_count=int(normalized_incremental["worker_count"]),
                incremental_poll_interval_ms=int(normalized_incremental["poll_interval_ms"]),
                error_code=error.code,
            )
        raise error from exc

    if observe:
        _emit_config_event(
            target,
            event_name="config.write",
            operation="write_default_config",
            outcome="written",
            status="ok",
            started=started,
            config_exists=True,
            has_compile_database=compile_database is not None,
            compile_database_scope=scope,
            clang_executable_scope=clang_executable_scope,
            libclang_library_scope=libclang_library_scope,
            gcc_executable_scope=gcc_executable_scope,
            clang_arg_count=clang_arg_count,
            extractor_worker_count=config.extractor_worker_count,
            incremental_enabled=config.incremental_temporary_enabled,
            incremental_worker_count=config.incremental_worker_count,
            incremental_poll_interval_ms=config.incremental_poll_interval_ms,
        )
    return config


def normalize_compile_database_path(target_repo: Path, value: Union[str, Path]) -> Path:
    _target_resolved, cipher_resolved = _validate_cipher_dir(Path(target_repo))
    return _normalize_compile_database_path(Path(target_repo), value, cipher_resolved)


def _normalize_compile_database_path(target: Path, value: Union[str, Path], cipher_resolved: Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise ConfigError("invalid_config", "paths.compile_database must be a string or null")
    raw_value = str(value)
    if os.name != "nt":
        raw_value = raw_value.replace("\\", "/")
    if raw_value == "":
        raise ConfigError("compile_database_unreadable", "compile database path must be a readable file")

    raw_path = Path(raw_value)
    candidate = raw_path if raw_path.is_absolute() else target / raw_path
    resolved = candidate.resolve(strict=False)
    if _is_relative_to(resolved, cipher_resolved):
        raise ConfigError("path_escape", "compile database cannot be inside target .cipher directory", path=candidate)
    if not resolved.is_file() or not os.access(str(resolved), os.R_OK):
        raise ConfigError("compile_database_unreadable", "compile database path must be a readable file", path=candidate)
    return resolved


def _normalize_libclang_library_path(target: Path, value: Optional[Union[str, Path]], cipher_resolved: Path) -> Optional[Path]:
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise ConfigError("invalid_config", "extractor.code.libclang_library must be a string or null")
    raw_value = str(value)
    if os.name != "nt":
        raw_value = raw_value.replace("\\", "/")
    if raw_value == "" or "\x00" in raw_value:
        raise ConfigError("invalid_config", "extractor.code.libclang_library must be a non-empty string or null")
    raw_path = Path(raw_value)
    candidate = raw_path if raw_path.is_absolute() else target / raw_path
    resolved = candidate.resolve(strict=False)
    if _is_relative_to(resolved, cipher_resolved):
        raise ConfigError("path_escape", "libclang library cannot be inside target .cipher directory", path=candidate)
    if not resolved.is_file() or not os.access(str(resolved), os.R_OK):
        raise ConfigError("libclang_unavailable", "libclang library path must be a readable file", path=candidate)
    return resolved


def safe_cipher_path(target_repo: Path, *parts: str) -> Path:
    target = Path(target_repo)
    target_resolved, cipher_resolved = _validate_cipher_dir(target)
    return _safe_cipher_path_with_resolved(target, target_resolved, cipher_resolved, *parts)


def _validate_cipher_dir(target: Path) -> Tuple[Path, Path]:
    target_resolved = target.resolve(strict=False)
    cipher_resolved = (target_resolved / ".cipher").resolve(strict=False)
    if not _is_relative_to(cipher_resolved, target_resolved):
        raise ConfigError("path_escape", ".cipher directory escapes target repository", path=target / ".cipher")
    return target_resolved, cipher_resolved


def _safe_cipher_path_with_resolved(
    target: Path,
    target_resolved: Path,
    cipher_resolved: Path,
    *parts: str,
) -> Path:
    candidate = target_resolved / ".cipher"
    for part in parts:
        if not isinstance(part, str) or part == "":
            raise ConfigError("invalid_config", "cipher path parts must be non-empty strings")
        candidate = candidate / part
    resolved = candidate.resolve(strict=False)
    if not _is_relative_to(resolved, cipher_resolved):
        raise ConfigError("path_escape", "generated path escapes target .cipher directory", path=target / ".cipher")
    return resolved


def _make_config(
    target: Path,
    compile_database_path: Optional[Path],
    clang_executable: Optional[str] = None,
    libclang_library_path: Optional[Path] = None,
    gcc_executable: Optional[str] = None,
    clang_args: Optional[List[str]] = None,
    extractor_worker_count: Optional[int] = None,
    incremental: Optional[Dict[str, Any]] = None,
) -> CipherConfig:
    normalized_extractor_worker_count = _normalize_extractor_worker_count(extractor_worker_count)
    incremental_values = _normalize_incremental(incremental)
    return CipherConfig(
        schema_version=SCHEMA_VERSION,
        target_repo=target,
        config_path=target / ".cipher" / CONFIG_FILENAME,
        cipher_dir=target / ".cipher",
        storage_snapshot_dir=target / ".cipher" / "snapshots",
        log_dir=target / ".cipher" / "log",
        compile_database_path=compile_database_path,
        clang_executable=clang_executable,
        libclang_library_path=libclang_library_path,
        gcc_executable=gcc_executable,
        clang_args=list(clang_args or []),
        extractor_worker_count=normalized_extractor_worker_count,
        incremental_temporary_enabled=bool(incremental_values["temporary_enabled"]),
        incremental_poll_interval_ms=int(incremental_values["poll_interval_ms"]),
        incremental_debounce_ms=int(incremental_values["debounce_ms"]),
        incremental_worker_count=int(incremental_values["worker_count"]),
        incremental_overlay_ttl_seconds=int(incremental_values["overlay_ttl_seconds"]),
        incremental_max_dirty_files=int(incremental_values["max_dirty_files"]),
    )


def _config_from_mapping(target: Path, mapping: Dict[str, Any], cipher_resolved: Path) -> Tuple[CipherConfig, str, int]:
    if not isinstance(mapping, dict):
        raise ConfigError("invalid_config", "config root must be a mapping")
    schema_version = mapping.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ConfigError("invalid_config", "schema_version must be an integer")
    if schema_version != SCHEMA_VERSION:
        raise ConfigError("unsupported_schema_version", "unsupported config schema version")
    paths = mapping.get("paths", {})
    if not isinstance(paths, dict):
        raise ConfigError("invalid_config", "paths must be a mapping")
    compile_database_value = paths.get("compile_database")
    scope = _compile_database_scope(compile_database_value)
    extractor = mapping.get("extractor", {})
    if not isinstance(extractor, dict):
        raise ConfigError("invalid_config", "extractor must be a mapping")
    code = extractor.get("code", {})
    if not isinstance(code, dict):
        raise ConfigError("invalid_config", "extractor.code must be a mapping")
    extractor_worker_count = extractor.get("worker_count")
    clang_executable = _normalize_tool_executable(
        target,
        code.get("clang_executable"),
        cipher_resolved,
        config_key="extractor.code.clang_executable",
        unavailable_code="clang_unavailable",
        tool_name="clang",
    )
    libclang_library_path = _normalize_libclang_library_path(target, code.get("libclang_library"), cipher_resolved)
    gcc_executable = _normalize_tool_executable(
        target,
        code.get("gcc_executable"),
        cipher_resolved,
        config_key="extractor.code.gcc_executable",
        unavailable_code="gcc_unavailable",
        tool_name="gcc",
    )
    clang_args = _normalize_clang_args(code.get("clang_args", []))
    incremental = _normalize_incremental(mapping.get("incremental", {}))
    legacy_section_count = sum(1 for key in ("graph", "inference") if key in mapping)
    if compile_database_value is None:
        return (
            _make_config(
                target,
                None,
                clang_executable,
                libclang_library_path,
                gcc_executable,
                clang_args,
                extractor_worker_count,
                incremental,
            ),
            scope,
            legacy_section_count,
        )
    return (
        _make_config(
            target,
            _normalize_compile_database_path(target, compile_database_value, cipher_resolved),
            clang_executable,
            libclang_library_path,
            gcc_executable,
            clang_args,
            extractor_worker_count,
            incremental,
        ),
        scope,
        legacy_section_count,
    )


def _default_mapping() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "paths": {"compile_database": None},
        "extractor": {
            "worker_count": None,
            "code": {"clang_executable": None, "libclang_library": None, "gcc_executable": None, "clang_args": []},
        },
        "incremental": dict(INCREMENTAL_DEFAULTS),
    }


def _read_config_file(path: Path) -> Dict[str, Any]:
    try:
        return _parse_mapping(path.read_text(encoding="utf-8"))
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("config_unreadable", "failed to read config file", path=path) from exc
    except UnicodeError as exc:
        raise ConfigError("invalid_config", "config file must be UTF-8 text", path=path) from exc


def _apply_overrides(mapping: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(overrides, dict):
        raise ConfigError("invalid_config", "overrides must be a mapping")
    merged: Dict[str, Any] = {
        "schema_version": mapping.get("schema_version"),
        "paths": dict(mapping.get("paths", {})),
        "extractor": {
            "worker_count": (mapping.get("extractor") or {}).get("worker_count"),
            "code": dict((mapping.get("extractor") or {}).get("code", {})),
        },
        "incremental": dict(mapping.get("incremental", {})),
    }
    for key, value in overrides.items():
        if key == "schema_version":
            merged["schema_version"] = value
        elif key == "paths":
            if not isinstance(value, dict):
                raise ConfigError("invalid_config", "overrides.paths must be a mapping")
            merged["paths"].update(value)
        elif key == "extractor":
            if not isinstance(value, dict):
                raise ConfigError("invalid_config", "overrides.extractor must be a mapping")
            unknown = set(value) - {"worker_count", "code"}
            if unknown:
                raise ConfigError("invalid_config", f"unknown extractor override: {sorted(unknown)[0]}")
            code = value.get("code", {})
            if not isinstance(code, dict):
                raise ConfigError("invalid_config", "overrides.extractor.code must be a mapping")
            if "worker_count" in value:
                merged["extractor"]["worker_count"] = value["worker_count"]
            merged["extractor"]["code"].update(code)
        elif key == "incremental":
            if not isinstance(value, dict):
                raise ConfigError("invalid_config", "overrides.incremental must be a mapping")
            merged["incremental"].update(value)
        else:
            raise ConfigError("invalid_config", f"unknown config override: {key}")
    return merged


def _parse_mapping(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    paths: Optional[Dict[str, Any]] = None
    extractor: Optional[Dict[str, Any]] = None
    code: Optional[Dict[str, Any]] = None
    incremental: Optional[Dict[str, Any]] = None
    current_section: Optional[str] = None
    current_list: Optional[str] = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith("      - "):
            if current_list != "clang_args" or code is None:
                raise ConfigError("invalid_config", "unsupported config indentation")
            code.setdefault("clang_args", []).append(_parse_scalar(raw_line.strip()[2:].strip()))
            continue
        if raw_line.startswith("    "):
            if current_section == "legacy":
                continue
            if current_section != "extractor.code" or code is None:
                raise ConfigError("invalid_config", "unsupported config indentation")
            key, value = _parse_key_value(raw_line.strip())
            if key == "clang_executable":
                code[key] = _parse_scalar(value)
                current_list = None
            elif key == "libclang_library":
                code[key] = _parse_scalar(value)
                current_list = None
            elif key == "gcc_executable":
                code[key] = _parse_scalar(value)
                current_list = None
            elif key == "clang_args":
                if value == "":
                    code[key] = []
                    current_list = "clang_args"
                else:
                    code[key] = _parse_inline_list(value)
                    current_list = None
            else:
                raise ConfigError("invalid_config", f"unknown extractor.code key: {key}")
            continue
        if raw_line.startswith("  "):
            if current_section == "extractor.code":
                key, value = _parse_key_value(raw_line.strip())
                if key != "worker_count":
                    raise ConfigError("invalid_config", "extractor.code entries must use four-space indentation")
                if extractor is None:
                    raise ConfigError("invalid_config", "extractor section is missing")
                extractor[key] = _parse_scalar(value)
                current_section = "extractor"
                current_list = None
                continue
            if current_section == "paths":
                key, value = _parse_key_value(raw_line.strip())
                if key != "compile_database":
                    raise ConfigError("invalid_config", f"unknown paths key: {key}")
                if paths is None:
                    raise ConfigError("invalid_config", "paths section is missing")
                paths[key] = _parse_scalar(value)
                current_list = None
                continue
            if current_section == "incremental":
                key, value = _parse_key_value(raw_line.strip())
                if incremental is None:
                    raise ConfigError("invalid_config", "incremental section is missing")
                if key not in INCREMENTAL_DEFAULTS:
                    raise ConfigError("invalid_config", f"unknown incremental key: {key}")
                incremental[key] = _parse_scalar(value)
                current_list = None
                continue
            if current_section == "legacy":
                continue
            if current_section == "extractor":
                key, value = _parse_key_value(raw_line.strip())
                if extractor is None:
                    raise ConfigError("invalid_config", "extractor section is missing")
                if key == "worker_count":
                    extractor[key] = _parse_scalar(value)
                    current_list = None
                    continue
                if key != "code" or value.strip():
                    raise ConfigError("invalid_config", "extractor only supports worker_count and code mapping")
                code = {}
                extractor[key] = code
                current_section = "extractor.code"
                current_list = None
                continue
            raise ConfigError("invalid_config", "unsupported config indentation")
            continue

        key, value = _parse_key_value(raw_line)
        current_section = None
        current_list = None
        if key == "schema_version":
            root[key] = _parse_scalar(value)
        elif key == "paths":
            if value.strip():
                raise ConfigError("invalid_config", "paths must be a mapping")
            paths = {}
            root[key] = paths
            current_section = "paths"
        elif key == "extractor":
            if value.strip():
                raise ConfigError("invalid_config", "extractor must be a mapping")
            extractor = {}
            root[key] = extractor
            current_section = "extractor"
        elif key == "incremental":
            if value.strip():
                raise ConfigError("invalid_config", "incremental must be a mapping")
            incremental = {}
            root[key] = incremental
            current_section = "incremental"
        elif key == "graph":
            if value.strip():
                raise ConfigError("invalid_config", "graph must be a mapping")
            root[key] = {}
            current_section = "legacy"
        elif key == "inference":
            if value.strip():
                raise ConfigError("invalid_config", "inference must be a mapping")
            root[key] = {}
            current_section = "legacy"
        else:
            raise ConfigError("invalid_config", f"unknown config key: {key}")

    if "schema_version" not in root:
        raise ConfigError("invalid_config", "schema_version is required")
    if "paths" not in root:
        root["paths"] = {}
    if "extractor" not in root:
        root["extractor"] = {
            "worker_count": None,
            "code": {"clang_executable": None, "libclang_library": None, "gcc_executable": None, "clang_args": []},
        }
    elif "code" not in root["extractor"]:
        root["extractor"]["code"] = {"clang_executable": None, "libclang_library": None, "gcc_executable": None, "clang_args": []}
    elif "libclang_library" not in root["extractor"]["code"]:
        root["extractor"]["code"]["libclang_library"] = None
    if "worker_count" not in root["extractor"]:
        root["extractor"]["worker_count"] = None
    if "incremental" not in root:
        root["incremental"] = {}
    return root


def _parse_key_value(line: str) -> Tuple[str, str]:
    if ":" not in line:
        raise ConfigError("invalid_config", "config line must contain ':'")
    key, value = line.split(":", 1)
    key = key.strip()
    if not key:
        raise ConfigError("invalid_config", "config key must be non-empty")
    return key, value.strip()


def _parse_scalar(value: str) -> Any:
    if value in {"", "null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "false", "False"}:
        return value.lower() == "true"
    if value in {"[", "]", "{", "}"} or value.startswith("[") or value.startswith("{"):
        raise ConfigError("invalid_config", "only scalar YAML values are supported")
    if value.isdigit():
        return int(value)
    return value


def _parse_inline_list(value: str) -> List[Any]:
    if value == "[]":
        return []
    raise ConfigError("invalid_config", "only empty inline lists are supported")


def _dump_mapping(mapping: Dict[str, JSONValue]) -> str:
    paths = mapping.get("paths")
    compile_database = None
    if isinstance(paths, dict):
        compile_database = paths.get("compile_database")
    extractor = mapping.get("extractor") if isinstance(mapping.get("extractor"), dict) else {}
    code = extractor.get("code", {}) if isinstance(extractor, dict) else {}
    extractor_worker_count = extractor.get("worker_count") if isinstance(extractor, dict) else None
    clang_executable = code.get("clang_executable") if isinstance(code, dict) else None
    libclang_library = code.get("libclang_library") if isinstance(code, dict) else None
    gcc_executable = code.get("gcc_executable") if isinstance(code, dict) else None
    clang_args = code.get("clang_args", []) if isinstance(code, dict) else []
    incremental = _normalize_incremental(mapping.get("incremental", {}))
    compile_database_value = "" if compile_database is None else str(compile_database)
    extractor_worker_count_value = (
        "" if extractor_worker_count is None else str(_normalize_extractor_worker_count(extractor_worker_count))
    )
    clang_executable_value = "" if clang_executable is None else str(clang_executable)
    libclang_library_value = "" if libclang_library is None else str(libclang_library)
    gcc_executable_value = "" if gcc_executable is None else str(gcc_executable)
    lines = [
        "# 配置 schema 版本；只有持久配置形状变化时才升级。",
        f"schema_version: {mapping['schema_version']}",
        "# 路径类输入，只保存路径本身。",
        "paths:",
        "  # 可选 compile_commands.json 路径；config 只保存路径，不解析文件内容。",
        f"  compile_database: {compile_database_value}",
        "# 全量 init/rebuild 的 extractor 运行设置。",
        "extractor:",
        "  # 全量抽取 worker 数；留空表示 auto，支持范围 1..32。",
        f"  worker_count: {extractor_worker_count_value}",
        "  # C extractor 工具链输入。",
        "  code:",
        "    # 可选 clang 可执行文件；留空保持配置可迁移，由运行时自动定位。",
        f"    clang_executable: {clang_executable_value}",
        "    # 可选 libclang 库兜底路径；正常路径从工具链自动定位。",
        f"    libclang_library: {libclang_library_value}",
        "    # 可选 GCC 可执行文件；当前 AST-only 路径不要求 GCC。",
        f"    gcc_executable: {gcc_executable_value}",
        "    # 额外 clang 参数；目标源码参数前会先应用这些全局参数。",
        "    clang_args:",
    ]
    if isinstance(clang_args, list):
        for arg in clang_args:
            lines.append(f"      - {arg}")
    lines.extend(
        [
            "# MCP/runtime views 使用的临时增量 overlay 设置。",
            "incremental:",
            "  # 是否启用不移动 snapshots/current 的临时 overlay 更新。",
            f"  temporary_enabled: {str(incremental['temporary_enabled']).lower()}",
            "  # 文件轮询间隔，单位毫秒。",
            f"  poll_interval_ms: {incremental['poll_interval_ms']}",
            "  # 脏文件事件防抖时间，单位毫秒。",
            f"  debounce_ms: {incremental['debounce_ms']}",
            "  # v1 保留兼容字段；校验并上报，但临时增量 active worker 固定为 1。",
            f"  worker_count: {incremental['worker_count']}",
            "  # 临时 overlay 条目过期秒数。",
            f"  overlay_ttl_seconds: {incremental['overlay_ttl_seconds']}",
            "  # 单次 overlay pass 最多处理的脏文件数。",
            f"  max_dirty_files: {incremental['max_dirty_files']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _normalize_incremental(value: Optional[Dict[str, Any]]) -> Dict[str, JSONValue]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError("invalid_config", "incremental must be a mapping")
    unknown = set(value) - set(INCREMENTAL_DEFAULTS)
    if unknown:
        raise ConfigError("invalid_config", f"unknown incremental key: {sorted(unknown)[0]}")
    normalized: Dict[str, JSONValue] = dict(INCREMENTAL_DEFAULTS)
    normalized.update(value)
    if not isinstance(normalized["temporary_enabled"], bool):
        raise ConfigError("invalid_config", "incremental.temporary_enabled must be a bool")
    for key, (lower, upper) in INCREMENTAL_RANGES.items():
        item = normalized[key]
        if not isinstance(item, int) or isinstance(item, bool) or not lower <= item <= upper:
            raise ConfigError("invalid_config", f"incremental.{key} must be between {lower} and {upper}")
    return normalized


def _normalize_extractor_worker_count(value: Any) -> int:
    if value is None:
        return _auto_extractor_worker_count()
    lower, upper = EXTRACTOR_WORKER_COUNT_RANGE
    if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
        raise ConfigError("invalid_config", f"extractor.worker_count must be between {lower} and {upper}")
    return value


def _auto_extractor_worker_count() -> int:
    return min(os.cpu_count() or 1, EXTRACTOR_WORKER_COUNT_RANGE[1])


def _serialize_compile_database(target: Path, path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    target_resolved = target.resolve(strict=False)
    path_resolved = path.resolve(strict=False)
    if _is_relative_to(path_resolved, target_resolved):
        return path_resolved.relative_to(target_resolved).as_posix()
    return str(path)


def _serialize_tool_executable(target: Path, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        return value
    target_resolved = target.resolve(strict=False)
    path_resolved = path.resolve(strict=False)
    if _is_relative_to(path_resolved, target_resolved):
        return path_resolved.relative_to(target_resolved).as_posix()
    return value


def _normalize_tool_executable(
    target: Path,
    value: Optional[Union[str, Path]],
    cipher_resolved: Path,
    *,
    config_key: str,
    unavailable_code: str,
    tool_name: str,
) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise ConfigError("invalid_config", f"{config_key} must be a string or null")
    raw_value = str(value)
    if raw_value == "" or "\x00" in raw_value:
        raise ConfigError("invalid_config", f"{config_key} must be a non-empty string or null")
    normalized = raw_value.replace("\\", "/") if os.name != "nt" else raw_value
    raw_path = Path(normalized)
    is_path = raw_path.is_absolute() or "/" in normalized
    if not is_path:
        return raw_value
    candidate = raw_path if raw_path.is_absolute() else target / raw_path
    resolved = candidate.resolve(strict=False)
    if _is_relative_to(resolved, cipher_resolved):
        raise ConfigError("path_escape", f"{tool_name} executable cannot be inside target .cipher directory", path=candidate)
    if not resolved.is_file() or not os.access(str(resolved), os.X_OK):
        raise ConfigError(unavailable_code, f"{tool_name} executable must be executable", path=candidate)
    return str(resolved)


def _normalize_clang_args(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("invalid_config", "extractor.code.clang_args must be a list")
    normalized = []
    for arg in value:
        if not isinstance(arg, str) or "\x00" in arg:
            raise ConfigError("invalid_config", "clang args must be strings without NUL")
        if arg in {"-o", ">", "1>", "2>"} or arg.startswith("-o") or ">" in arg:
            raise ConfigError("invalid_config", "clang args must not configure output files")
        normalized.append(arg)
    return normalized


def _compile_database_scope(value: Optional[Union[str, Path]]) -> str:
    if value is None:
        return "none"
    if not isinstance(value, (str, Path)):
        return "invalid"
    raw_value = str(value)
    if raw_value == "":
        return "invalid"
    normalized = raw_value.replace("\\", "/") if os.name != "nt" else raw_value
    return "absolute" if Path(normalized).is_absolute() else "relative"


def _tool_input_scope(value: Optional[Union[str, Path]]) -> str:
    if value is None:
        return "none"
    if not isinstance(value, (str, Path)):
        return "invalid"
    return _tool_scope(str(value))


def _tool_scope(value: Optional[str]) -> str:
    if value is None:
        return "none"
    if not isinstance(value, str) or value == "":
        return "invalid"
    normalized = value.replace("\\", "/") if os.name != "nt" else value
    path = Path(normalized)
    if path.is_absolute():
        return "absolute"
    if "/" in normalized:
        return "relative"
    return "command"


def _emit_config_event(
    target: Path,
    *,
    event_name: str,
    operation: str,
    outcome: str,
    status: str,
    started: float,
    config_exists: bool,
    has_compile_database: bool,
    compile_database_scope: str,
    clang_executable_scope: str,
    libclang_library_scope: str,
    gcc_executable_scope: str,
    clang_arg_count: int,
    extractor_worker_count: int,
    incremental_enabled: bool,
    incremental_worker_count: int,
    incremental_poll_interval_ms: int,
    legacy_section_count: int = 0,
    error_code: Optional[str] = None,
) -> None:
    try:
        safe_cipher_path(target, "log", "config.jsonl")
        open_log(target).write_event(
            LogEvent(
                event_name=event_name,
                channel="config",
                status=status,
                duration_ms=max(0.0, (time.perf_counter() - started) * 1000),
                error_code=error_code,
                summary=f"{operation} {outcome}",
                payload={
                    "operation": operation,
                    "outcome": outcome,
                    "has_compile_database": has_compile_database,
                    "compile_database_scope": compile_database_scope,
                    "clang_executable_scope": clang_executable_scope,
                    "libclang_library_scope": libclang_library_scope,
                    "gcc_executable_scope": gcc_executable_scope,
                    "clang_arg_count": clang_arg_count,
                    "extractor_worker_count": extractor_worker_count,
                    "incremental_enabled": incremental_enabled,
                    "incremental_worker_count": incremental_worker_count,
                    "incremental_poll_interval_ms": incremental_poll_interval_ms,
                    "legacy_section_count": legacy_section_count,
                    "config_exists": config_exists,
                    "error_code": error_code,
                },
            )
        )
    except (ConfigError, LogError):
        pass


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


__all__ = [
    "CipherConfig",
    "ConfigError",
    "load_config",
    "normalize_compile_database_path",
    "safe_cipher_path",
    "write_default_config",
]

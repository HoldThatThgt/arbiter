"""Command line interface for cipher-2."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Union

from cipher2 import __version__
from cipher2.common import JSONValue
from cipher2.config import CipherConfig, ConfigError, load_config, write_default_config
from cipher2.initializer import InitError, InitStageTiming, InitSummary, initialize_repository, preflight_build_readiness
from cipher2.initializer.progress import InitProgressEvent
from cipher2.storage import StorageError
from cipher2.tools.log import LogError, LogEvent, open_log
from cipher2.tools.views import ToolsOverviewModel, build_overview


COMMANDS = {"init", "rebuild", "status"}


@dataclass(frozen=True)
class CliArgs:
    command: str
    target_repo: Path
    source_roots: List[Path]
    profile: str
    compile_database: Optional[Path]
    no_mcp_config: bool
    print_mcp_config: bool
    log_enabled: bool
    progress_enabled: bool
    json_output: bool
    stderr_is_tty: bool = False


@dataclass(frozen=True)
class StatusCliArgs:
    command: str
    target_repo: Path
    json_output: bool


@dataclass(frozen=True)
class CliError:
    code: str
    message: str
    details: Dict[str, JSONValue]

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class CliWarning:
    code: str
    message: str
    source: Optional[str]
    details: Dict[str, JSONValue]

    def to_json(self) -> Dict[str, JSONValue]:
        return {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class CliSetup:
    compile_database: Dict[str, JSONValue]
    toolchain: Dict[str, JSONValue]
    mcp_config: Dict[str, JSONValue]
    warnings: Sequence[CliWarning] = ()
    printed_mcp_config: Optional[Dict[str, JSONValue]] = None

    def to_json(self) -> Dict[str, JSONValue]:
        row: Dict[str, JSONValue] = {
            "compile_database": dict(self.compile_database),
            "toolchain": dict(self.toolchain),
            "mcp_config": dict(self.mcp_config),
        }
        if self.warnings:
            row["warnings"] = [warning.to_json() for warning in self.warnings]
        if self.printed_mcp_config is not None:
            row["printed_mcp_config"] = dict(self.printed_mcp_config)
        return row


@dataclass(frozen=True)
class CliResult:
    ok: bool
    exit_code: int
    command: str
    snapshot_id: Optional[str]
    fact_count: int
    relative_count: int
    source_count: int
    warning_count: int
    duration_ms: float
    stage_timings: Sequence[InitStageTiming] = ()
    error: Optional[CliError] = None
    warnings: Sequence[CliWarning] = ()
    setup: Optional[CliSetup] = None

    def to_json(self) -> Dict[str, JSONValue]:
        row: Dict[str, JSONValue] = {
            "command": self.command,
            "duration_ms": self.duration_ms,
            "fact_count": self.fact_count,
            "ok": self.ok,
            "relative_count": self.relative_count,
            "snapshot_id": self.snapshot_id,
            "stage_timings": [stage.to_json() for stage in self.stage_timings],
            "source_count": self.source_count,
            "warning_count": self.warning_count,
        }
        if self.error is not None:
            row["exit_code"] = self.exit_code
            row["error"] = self.error.to_json()
        if self.warnings:
            row["warnings"] = [warning.to_json() for warning in self.warnings]
        if self.setup is not None:
            row["setup"] = self.setup.to_json()
        return row


@dataclass(frozen=True)
class StatusCliResult:
    ok: bool
    exit_code: int
    command: str
    overview: Optional[ToolsOverviewModel]
    error: Optional[CliError]
    duration_ms: float


@dataclass(frozen=True)
class StatusRenderOptions:
    json_output: bool
    top_n: int = 5


class _UsageError(Exception):
    def __init__(self, message: str, usage: str) -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage


class _NoExitParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message, self.format_usage())


class _InitProgressReporter:
    def __init__(
        self,
        command: str,
        stream: Optional[TextIO],
        *,
        enabled: bool,
        tty: bool,
        min_interval_ms: float = 5000.0,
    ) -> None:
        self.command = command
        self.stream = stream
        self.enabled = bool(enabled and stream is not None)
        self.tty = tty
        self.min_interval_ms = min_interval_ms
        self.started = time.perf_counter()
        self.total: Optional[int] = None
        self.processed = 0
        self.current_source: Optional[str] = None
        self.warning_count = 0
        self.partial_ast_count = 0
        self.compile_hit_count = 0
        self.compile_miss_count = 0
        self.profile = "default"
        self.compile_db_label = "not_configured"
        self.clang_label = "-"
        self._last_render_ms = -min_interval_ms
        self._last_line_len = 0
        self._emitted = False

    @property
    def sink(self):
        return self.handle if self.enabled else None

    def handle(self, event: InitProgressEvent) -> None:
        if not self.enabled:
            return
        if event.kind == "sources_planned":
            self.total = event.total
            profile = event.payload.get("profile")
            if isinstance(profile, str) and profile:
                self.profile = profile
            self.compile_db_label = (
                "configured" if event.payload.get("compile_database_configured") is True else "not_configured"
            )
            self._render(self._format_start_line(), force=True)
            return
        if event.kind == "compile_database":
            self.compile_db_label = "configured"
            indexed = _int_count(event.counts, "compile_command_indexed_source_count")
            if indexed > 0:
                self.compile_db_label = f"configured indexed={indexed}"
            self._render(self._format_start_line(), force=True)
            return
        if event.kind == "toolchain":
            vendor = event.payload.get("clang_vendor")
            version = event.payload.get("clang_version")
            if isinstance(vendor, str) and isinstance(version, str):
                self.clang_label = f"{vendor} {version}".strip()
            elif isinstance(version, str):
                self.clang_label = version
            self._render(self._format_start_line(), force=True)
            return
        if event.kind != "file_done":
            return
        self.processed += 1
        self.current_source = event.source
        self.warning_count += _int_count(event.counts, "warning_count")
        self.partial_ast_count += _int_count(event.counts, "partial_ast_count")
        self.compile_hit_count += _int_count(event.counts, "compile_command_hit_count")
        self.compile_miss_count += _int_count(event.counts, "compile_command_miss_count")
        self._render(self._format_file_line(), force=self.tty or self._is_complete())

    def finish_success(self, result: CliResult) -> None:
        if not self.enabled:
            return
        total = self.total if self.total is not None else result.source_count
        files = f"{self.processed}/{total}" if total is not None else str(self.processed)
        line = (
            f"cipher2 {self.command}: done files={files} facts={result.fact_count} "
            f"relatives={result.relative_count} warnings={result.warning_count} "
            f"partial_ast={self.partial_ast_count} compile_db_hit={self.compile_hit_count} "
            f"compile_db_miss={self.compile_miss_count} elapsed={self._elapsed_seconds()}s"
        )
        self._write_final_line(line)

    def finish_error(self) -> None:
        if not self.enabled or not self._emitted:
            return
        self._write_final_line(f"cipher2 {self.command}: stopped elapsed={self._elapsed_seconds()}s")

    def _is_complete(self) -> bool:
        return self.total is not None and self.processed >= self.total

    def _format_start_line(self) -> str:
        sources = "-" if self.total is None else str(self.total)
        return (
            f"cipher2 {self.command}: sources={sources} compile_db={self.compile_db_label} "
            f"clang={self.clang_label} profile={self.profile} elapsed={self._elapsed_seconds()}s"
        )

    def _format_file_line(self) -> str:
        if self.total is None:
            progress = f"processed={self.processed}"
        else:
            progress = f"{self.processed}/{self.total}"
        source = _truncate_middle(self.current_source or "-", 96)
        return (
            f"cipher2 {self.command}: {progress} {source} elapsed={self._elapsed_seconds()}s "
            f"warnings={self.warning_count} partial_ast={self.partial_ast_count}"
        )

    def _render(self, line: str, *, force: bool = False) -> None:
        if not self.enabled:
            return
        elapsed_ms = _elapsed_ms(self.started)
        if not self.tty and not force and elapsed_ms - self._last_render_ms < self.min_interval_ms:
            return
        if self.tty:
            self._write_tty_line(line)
        else:
            self._safe_write(f"{line}\n")
        self._last_render_ms = elapsed_ms
        self._emitted = True

    def _write_tty_line(self, line: str) -> None:
        padding = " " * max(0, self._last_line_len - len(line))
        self._safe_write(f"\r{line}{padding}")
        self._last_line_len = len(line)

    def _write_final_line(self, line: str) -> None:
        if self.tty:
            padding = " " * max(0, self._last_line_len - len(line))
            self._safe_write(f"\r{line}{padding}\n")
            self._last_line_len = 0
        else:
            self._safe_write(f"{line}\n")
        self._emitted = True

    def _safe_write(self, text: str) -> None:
        if self.stream is None:
            self.enabled = False
            return
        try:
            self.stream.write(text)
        except (BrokenPipeError, OSError, ValueError):
            self.enabled = False

    def _elapsed_seconds(self) -> int:
        return max(0, int((time.perf_counter() - self.started)))


def parse_args(
    argv: Sequence[str],
    *,
    stderr_is_tty: bool = False,
) -> Union[CliArgs, StatusCliArgs]:
    command = argv[0] if argv and argv[0] in COMMANDS else "init"
    parser = _command_parser(command)
    values = parser.parse_args(list(argv[1:] if argv and argv[0] == command else argv))
    if values.target is None:
        raise _UsageError("the following arguments are required: target", parser.format_usage())
    if command == "status":
        return StatusCliArgs(
            command=command,
            target_repo=Path(values.target),
            json_output=values.json_output,
        )
    return CliArgs(
        command=command,
        target_repo=Path(values.target),
        source_roots=[Path(item) for item in values.source_roots],
        profile=values.profile,
        compile_database=Path(values.compile_database) if values.compile_database is not None else None,
        no_mcp_config=bool(getattr(values, "no_mcp_config", False)),
        print_mcp_config=bool(getattr(values, "print_mcp_config", False)),
        log_enabled=not values.no_log,
        progress_enabled=command == "init" and not getattr(values, "no_progress", False),
        json_output=values.json_output,
        stderr_is_tty=stderr_is_tty,
    )


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    inp = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    if _wants_root_help(args_list):
        out.write(_root_parser().format_help())
        return 0
    if _wants_command_help(args_list):
        out.write(_command_parser(args_list[0]).format_help())
        return 0
    if args_list in (["--version"], ["-V"]):
        out.write(f"cipher2 {__version__}\n")
        return 0
    if not args_list or args_list[0] not in COMMANDS:
        err.write(_root_parser().format_usage())
        err.write(f"cipher2: error: unknown command: {args_list[0] if args_list else ''}\n")
        return 2

    try:
        cli_args = parse_args(
            args_list,
            stderr_is_tty=_is_tty(err),
        )
    except _UsageError as exc:
        err.write(exc.usage)
        err.write(f"cipher2: error: {exc.message}\n")
        return 2

    if isinstance(cli_args, StatusCliArgs):
        status_result = run_status(cli_args)
        out.write(
            render_status_result(
                status_result,
                options=StatusRenderOptions(json_output=cli_args.json_output),
                target_repo=cli_args.target_repo,
            )
        )
        if status_result.error is not None:
            err.write(f"cipher2: {status_result.error.code}: {status_result.error.message}\n")
        return status_result.exit_code

    result = run_init(cli_args, stdin=inp, stdout=out, stderr=err)
    out.write(render_result(result, json_output=cli_args.json_output))
    if result.error is not None:
        err.write(f"cipher2: {result.error.code}: {result.error.message}\n")
    return result.exit_code


def run_status(args: StatusCliArgs) -> StatusCliResult:
    started = time.perf_counter()
    target = args.target_repo
    error = _validate_target(target)
    if error is not None:
        return StatusCliResult(
            ok=False,
            exit_code=1,
            command=args.command,
            overview=None,
            error=error,
            duration_ms=_elapsed_ms(started),
        )

    overview = build_overview(target, top_n=5)
    result = StatusCliResult(
        ok=True,
        exit_code=0,
        command=args.command,
        overview=overview,
        error=None,
        duration_ms=_elapsed_ms(started),
    )
    _emit_cli_status(target, result, args, started)
    return result


def run_init(
    args: CliArgs,
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> CliResult:
    started = time.perf_counter()
    target = args.target_repo
    error = _validate_target(target)
    if error is not None:
        return _error_result(args.command, error, started)
    if not isinstance(args.profile, str) or not args.profile.strip():
        error = CliError("invalid_profile", "profile must be a non-empty string", {})
        _emit_cli_error(target, error, args, started)
        return _error_result(args.command, error, started)
    progress = _InitProgressReporter(
        args.command,
        stderr,
        enabled=args.progress_enabled,
        tty=args.stderr_is_tty,
    )
    try:
        setup_builder = _prepare_config(args)
        preflight_build_readiness(target, log_enabled=args.log_enabled)
        progress_sink = _combine_progress_sinks(progress.sink, setup_builder.handle_progress)
        summary = initialize_repository(
            target,
            source_roots=[str(path) for path in args.source_roots] or None,
            profile=args.profile,
            log_enabled=args.log_enabled,
            progress_sink=progress_sink,
        )
        mcp_config, mcp_warning = _write_or_skip_mcp_config(target, args, setup_builder.mcp_server_config())
        setup_builder.mcp_config = mcp_config
        if mcp_warning is not None:
            setup_builder.warnings.append(mcp_warning)
    except ConfigError as exc:
        progress.finish_error()
        error = CliError(exc.code, exc.message, {})
        _emit_cli_error(target, error, args, started)
        return _error_result(args.command, error, started)
    except InitError as exc:
        progress.finish_error()
        error = CliError(exc.code, exc.message, dict(exc.details))
        _emit_cli_error(target, error, args, started)
        return _error_result(args.command, error, started)
    except StorageError as exc:
        progress.finish_error()
        error = CliError("storage_error", "failed to initialize storage", {"storage_code": exc.code})
        _emit_cli_error(target, error, args, started)
        return _error_result(args.command, error, started)

    result = _success_result(summary, args.command, started, setup=setup_builder.build())
    progress.finish_success(result)
    _emit_cli_command(target, result, args, started)
    _emit_cli_setup_events(target, result.setup, args, started)
    if args.command == "rebuild":
        _emit_rebuild_events(target, result, args, started)
    return result


def render_result(result: CliResult, *, json_output: bool) -> str:
    if json_output:
        return json.dumps(result.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if result.ok:
        verb = "initialized" if result.command == "init" else "rebuilt"
        lines = [
            f"{verb} snapshot={result.snapshot_id} facts={result.fact_count} "
            f"relatives={result.relative_count} sources={result.source_count} warnings={result.warning_count}\n"
        ]
        if result.stage_timings:
            lines.append(_render_stage_timings_line(result.stage_timings) + "\n")
        if result.command == "init" and result.setup is not None:
            lines.append(_render_setup_line(result.setup) + "\n")
            for warning in result.setup.warnings:
                lines.append(f"warning: {warning.code}: {warning.message}\n")
            if result.setup.printed_mcp_config is not None:
                lines.append("mcp_config:\n")
                lines.append(json.dumps(result.setup.printed_mcp_config, ensure_ascii=False, indent=2, sort_keys=True))
                lines.append("\n")
        return "".join(lines)
    return ""


def render_status_result(result: StatusCliResult, *, options: StatusRenderOptions, target_repo: Path) -> str:
    if not result.ok or result.overview is None:
        return ""
    if options.json_output:
        return json.dumps(asdict(result.overview), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return _render_status_human(target_repo, result.overview)


def _render_setup_line(setup: CliSetup) -> str:
    compile_db = setup.compile_database.get("path") or setup.compile_database.get("action") or "-"
    toolchain = _toolchain_label(setup.toolchain)
    mcp_action = setup.mcp_config.get("action", "-")
    mcp_path = setup.mcp_config.get("path", ".mcp.json")
    return (
        f"setup: compile_db={compile_db} clang={toolchain} "
        f"mcp={mcp_path}:{mcp_action} setup_warnings={len(setup.warnings)}"
    )


def _toolchain_label(toolchain: Dict[str, JSONValue]) -> str:
    status = toolchain.get("status")
    vendor = toolchain.get("clang_vendor")
    version = toolchain.get("clang_version")
    if isinstance(vendor, str) and isinstance(version, str):
        return f"{vendor}-{version}"
    if isinstance(version, str):
        return version
    if isinstance(status, str):
        return status
    return "not_run"


class _SetupBuilder:
    def __init__(self, target: Path, *, print_mcp_config: bool) -> None:
        self.target = target
        self.compile_database: Dict[str, JSONValue] = {
            "action": "not_configured",
            "configured": False,
            "candidate_count": 0,
        }
        self.toolchain: Dict[str, JSONValue] = {"status": "not_run"}
        self.mcp_config: Dict[str, JSONValue] = {"action": "pending"}
        self.warnings: List[CliWarning] = []
        self.print_mcp_config = print_mcp_config

    def mcp_server_config(self) -> Dict[str, JSONValue]:
        repo = str(self.target.resolve(strict=False))
        return {
            "mcpServers": {
                "cipher-2": {
                    "command": sys.executable,
                    "args": [
                        "-c",
                        f"from cipher2.mcp import serve_stdio; raise SystemExit(serve_stdio({repo!r}))",
                    ],
                }
            }
        }

    def handle_progress(self, event: InitProgressEvent) -> None:
        if event.kind != "toolchain":
            return
        payload = event.payload
        error_code = payload.get("error_code")
        status = "error" if isinstance(error_code, str) and error_code else "detected"
        row: Dict[str, JSONValue] = {
            "status": status,
            "backend": _json_scalar(payload.get("backend"), default="unknown"),
            "gcc_required": _json_bool(payload.get("gcc_required")),
            "gcc_checked": _json_bool(payload.get("gcc_checked")),
        }
        for key in (
            "clang_vendor",
            "clang_version",
            "libclang_version",
            "libclang_library_scope",
            "version_match",
            "type_driven_ast",
            "ast_json_supported",
        ):
            value = payload.get(key)
            if isinstance(value, (str, bool, int, float)) or value is None:
                row[key] = value
        if isinstance(error_code, str) and error_code:
            row["error_code"] = error_code
        self.toolchain = row

    def build(self) -> CliSetup:
        printed = self.mcp_server_config() if self.print_mcp_config else None
        return CliSetup(
            compile_database=dict(self.compile_database),
            toolchain=dict(self.toolchain),
            mcp_config=dict(self.mcp_config),
            warnings=tuple(self.warnings),
            printed_mcp_config=printed,
        )


def _prepare_config(args: CliArgs) -> _SetupBuilder:
    setup = _SetupBuilder(args.target_repo, print_mcp_config=args.print_mcp_config)
    config_path = args.target_repo / ".cipher" / "config.yml"
    if args.command != "init":
        _prepare_rebuild_config(args)
        setup.mcp_config = {"action": "not_applicable"}
        return setup

    if args.compile_database is not None:
        if config_path.exists():
            existing = load_config(args.target_repo, observe=args.log_enabled)
            _write_config_preserving(args, existing, args.compile_database)
        else:
            write_default_config(args.target_repo, compile_database=args.compile_database, observe=args.log_enabled)
        setup.compile_database = _compile_database_setup_row(
            "explicit",
            path=args.compile_database,
            target=args.target_repo,
            configured=True,
            candidate_count=0,
        )
        return setup

    if config_path.exists():
        existing = load_config(args.target_repo, observe=args.log_enabled)
        if existing.compile_database_path is not None:
            setup.compile_database = _compile_database_setup_row(
                "preserved",
                path=existing.compile_database_path,
                target=args.target_repo,
                configured=True,
                candidate_count=0,
            )
            return setup
        discovery = _discover_compile_database(args.target_repo)
        if discovery.selected_path is not None:
            _write_config_preserving(args, existing, _repo_relative_or_absolute(args.target_repo, discovery.selected_path))
            setup.compile_database = _compile_database_setup_row(
                "discovered",
                path=discovery.selected_path,
                target=args.target_repo,
                configured=True,
                candidate_count=discovery.candidate_count,
            )
            return setup
        setup.compile_database = _compile_database_setup_row(
            "not_found",
            path=None,
            target=args.target_repo,
            configured=False,
            candidate_count=discovery.candidate_count,
        )
        setup.warnings.append(_compile_database_missing_warning(discovery.candidate_count))
        return setup

    discovery = _discover_compile_database(args.target_repo)
    if discovery.selected_path is not None:
        write_default_config(
            args.target_repo,
            compile_database=_repo_relative_or_absolute(args.target_repo, discovery.selected_path),
            observe=args.log_enabled,
        )
        setup.compile_database = _compile_database_setup_row(
            "discovered",
            path=discovery.selected_path,
            target=args.target_repo,
            configured=True,
            candidate_count=discovery.candidate_count,
        )
        return setup

    write_default_config(args.target_repo, observe=args.log_enabled)
    setup.compile_database = _compile_database_setup_row(
        "not_found",
        path=None,
        target=args.target_repo,
        configured=False,
        candidate_count=discovery.candidate_count,
    )
    setup.warnings.append(_compile_database_missing_warning(discovery.candidate_count))
    return setup


def _prepare_rebuild_config(args: CliArgs) -> None:
    config_path = args.target_repo / ".cipher" / "config.yml"
    if args.compile_database is not None:
        if config_path.exists():
            existing = load_config(args.target_repo, observe=args.log_enabled)
            _write_config_preserving(args, existing, args.compile_database)
        else:
            write_default_config(args.target_repo, compile_database=args.compile_database, observe=args.log_enabled)
    elif not config_path.exists():
        write_default_config(args.target_repo, observe=args.log_enabled)


def _write_config_preserving(args: CliArgs, existing: CipherConfig, compile_database: Union[str, Path]) -> None:
    write_default_config(
        args.target_repo,
        compile_database=compile_database,
        clang_executable=existing.clang_executable,
        gcc_executable=existing.gcc_executable,
        libclang_library=existing.libclang_library_path,
        clang_args=existing.clang_args,
        extractor_worker_count=existing.extractor_worker_count,
        incremental=_incremental_mapping(existing),
        observe=args.log_enabled,
    )


@dataclass(frozen=True)
class _CompileDatabaseDiscovery:
    selected_path: Optional[Path]
    candidate_count: int


def _discover_compile_database(target: Path) -> _CompileDatabaseDiscovery:
    candidates: List[Path] = []
    seen = set()

    def add(path: Path) -> None:
        normalized = path.resolve(strict=False)
        if normalized in seen or not normalized.is_file():
            return
        seen.add(normalized)
        candidates.append(path)

    add(target / "compile_commands.json")
    add(target / "build" / "compile_commands.json")
    add(target / "out" / "compile_commands.json")
    for base in (target / "build", target / "out"):
        if base.is_dir():
            for child in sorted(base.iterdir(), key=lambda item: item.name):
                if child.is_dir():
                    add(child / "compile_commands.json")
    excluded = {".cipher", ".git", "node_modules", "vendor"}
    for child in sorted(target.iterdir(), key=lambda item: item.name):
        if child.name in excluded or not child.is_dir():
            continue
        for grandchild in sorted(child.iterdir(), key=lambda item: item.name):
            if grandchild.name in excluded or not grandchild.is_dir():
                continue
            add(grandchild / "compile_commands.json")
    return _CompileDatabaseDiscovery(selected_path=candidates[0] if candidates else None, candidate_count=len(candidates))


def _compile_database_setup_row(
    action: str,
    *,
    path: Optional[Union[str, Path]],
    target: Path,
    configured: bool,
    candidate_count: int,
) -> Dict[str, JSONValue]:
    row: Dict[str, JSONValue] = {
        "action": action,
        "configured": configured,
        "candidate_count": candidate_count,
    }
    if path is not None:
        row["path"] = _repo_relative_or_absolute(target, Path(path))
    return row


def _compile_database_missing_warning(candidate_count: int) -> CliWarning:
    return CliWarning(
        code="compile_database_not_found",
        message=(
            "compile_commands.json was not found; C AST quality may degrade without real include and macro flags. "
            "Generate it with CMake or Bear, or pass --compile-database."
        ),
        source=None,
        details={
            "candidate_count": candidate_count,
            "cmake_example": "cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build",
            "bear_example": "bear -- <build command>",
            "cli_example": "cipher2 init <repo> --compile-database <path>",
        },
    )


def _repo_relative_or_absolute(target: Path, path: Path) -> str:
    target_resolved = target.resolve(strict=False)
    path_resolved = path if path.is_absolute() else (target / path)
    path_resolved = path_resolved.resolve(strict=False)
    try:
        return path_resolved.relative_to(target_resolved).as_posix()
    except ValueError:
        return str(path)


def _combine_progress_sinks(*sinks: Optional[Callable[[InitProgressEvent], None]]) -> Optional[Callable[[InitProgressEvent], None]]:
    active = [sink for sink in sinks if sink is not None]
    if not active:
        return None

    def handle(event: InitProgressEvent) -> None:
        for sink in tuple(active):
            try:
                sink(event)
            except Exception:
                continue

    return handle


def _write_or_skip_mcp_config(
    target: Path,
    args: CliArgs,
    snippet: Dict[str, JSONValue],
) -> tuple[Dict[str, JSONValue], Optional[CliWarning]]:
    if args.command != "init":
        return {"action": "not_applicable"}, None
    if args.no_mcp_config:
        return {"action": "skipped", "path": ".mcp.json", "reason": "disabled"}, None
    path = target / ".mcp.json"
    server = snippet["mcpServers"]["cipher-2"]  # type: ignore[index]
    try:
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                warning = _mcp_warning("mcp_config_malformed", "existing .mcp.json is not valid JSON; leaving it unchanged")
                return {"action": "warning", "path": ".mcp.json", "warning_code": warning.code}, warning
            if not isinstance(current, dict):
                warning = _mcp_warning("mcp_config_malformed", "existing .mcp.json root must be an object; leaving it unchanged")
                return {"action": "warning", "path": ".mcp.json", "warning_code": warning.code}, warning
            existing_servers = current.get("mcpServers")
            if existing_servers is None:
                current["mcpServers"] = {}
            elif not isinstance(existing_servers, dict):
                warning = _mcp_warning("mcp_config_malformed", "existing .mcp.json mcpServers must be an object; leaving it unchanged")
                return {"action": "warning", "path": ".mcp.json", "warning_code": warning.code}, warning
            action = "updated"
        else:
            current = {}
            current["mcpServers"] = {}
            action = "created"
        current["mcpServers"]["cipher-2"] = server  # type: ignore[index]
        tmp_path = target / ".mcp.json.tmp"
        tmp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
        return {"action": action, "path": ".mcp.json", "server_name": "cipher-2"}, None
    except OSError:
        warning = _mcp_warning("mcp_config_write_failed", "failed to write repo-root .mcp.json")
        return {"action": "warning", "path": ".mcp.json", "warning_code": warning.code}, warning


def _mcp_warning(code: str, message: str) -> CliWarning:
    return CliWarning(code=code, message=message, source=None, details={})


def _json_scalar(value: object, *, default: str) -> JSONValue:
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return default


def _json_bool(value: object) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _incremental_mapping(config: CipherConfig) -> Dict[str, JSONValue]:
    return {
        "temporary_enabled": config.incremental_temporary_enabled,
        "poll_interval_ms": config.incremental_poll_interval_ms,
        "debounce_ms": config.incremental_debounce_ms,
        "worker_count": config.incremental_worker_count,
        "overlay_ttl_seconds": config.incremental_overlay_ttl_seconds,
        "max_dirty_files": config.incremental_max_dirty_files,
    }


def _validate_target(target: Path) -> Optional[CliError]:
    if not target.exists() or not target.is_dir() or not os.access(str(target), os.R_OK):
        return CliError("invalid_target", "target must be an existing readable directory", {})
    return None


def _success_result(summary: InitSummary, command: str, started: float, *, setup: Optional[CliSetup] = None) -> CliResult:
    return CliResult(
        ok=True,
        exit_code=0,
        command=command,
        snapshot_id=summary.snapshot_id,
        fact_count=summary.fact_count,
        relative_count=summary.relative_count,
        source_count=summary.source_count,
        warning_count=summary.warning_count,
        duration_ms=_elapsed_ms(started),
        stage_timings=tuple(summary.stage_timings),
        warnings=tuple(_warning_from_init_error(error) for error in summary.errors),
        setup=setup,
    )


def _error_result(command: str, error: CliError, started: float) -> CliResult:
    return CliResult(
        ok=False,
        exit_code=1,
        command=command,
        snapshot_id=None,
        fact_count=0,
        relative_count=0,
        source_count=0,
        warning_count=0,
        duration_ms=_elapsed_ms(started),
        error=error,
    )


def _render_stage_timings_line(stage_timings: Sequence[InitStageTiming]) -> str:
    if not stage_timings:
        return "stages: -"
    parts = [f"{stage.stage}={_fmt_duration_ms(stage.duration_ms)}" for stage in stage_timings]
    return "stages: " + " ".join(parts)


def _warning_from_init_error(error: InitError) -> CliWarning:
    return CliWarning(
        code=error.code,
        message=error.message,
        source=error.source,
        details=dict(error.details),
    )


def _emit_cli_command(target: Path, result: CliResult, args: CliArgs, started: float) -> None:
    if not args.log_enabled:
        return
    _write_cli_event(
        target,
        LogEvent(
            event_name="cli.command",
            channel="cli",
            status="ok",
            duration_ms=_elapsed_ms(started),
            summary=f"cipher2 {args.command} completed with {result.fact_count} facts",
            counts={
                "fact_count": result.fact_count,
                "relative_count": result.relative_count,
                "source_count": result.source_count,
                "warning_count": result.warning_count,
            },
            payload={
                "operation": args.command,
                "outcome": "completed",
                "command_name": args.command,
                "exit_code": 0,
                "json_output": args.json_output,
                "profile": args.profile,
                "source_root_count": len(args.source_roots),
            },
        ),
    )


def _emit_cli_setup_events(target: Path, setup: Optional[CliSetup], args: CliArgs, started: float) -> None:
    if not args.log_enabled or setup is None or args.command != "init":
        return
    compile_status = "warning" if setup.compile_database.get("action") == "not_found" else "ok"
    _write_cli_event(
        target,
        LogEvent(
            event_name="cli.setup_discovery",
            channel="cli",
            status=compile_status,
            duration_ms=_elapsed_ms(started),
            summary="init setup compile database discovery",
            counts={"warning_count": 1 if compile_status == "warning" else 0},
            error_code="compile_database_not_found" if compile_status == "warning" else None,
            payload={
                "operation": "init_setup",
                "outcome": setup.compile_database.get("action"),
                "compile_database_configured": setup.compile_database.get("configured"),
                "compile_database_candidate_count": setup.compile_database.get("candidate_count"),
                "error_code": "compile_database_not_found" if compile_status == "warning" else None,
            },
        ),
    )
    mcp_warning_code = setup.mcp_config.get("warning_code")
    _write_cli_event(
        target,
        LogEvent(
            event_name="cli.mcp_config",
            channel="cli",
            status="warning" if isinstance(mcp_warning_code, str) else "ok",
            duration_ms=_elapsed_ms(started),
            summary="init setup mcp config",
            counts={"warning_count": 1 if isinstance(mcp_warning_code, str) else 0},
            error_code=mcp_warning_code if isinstance(mcp_warning_code, str) else None,
            payload={
                "operation": "init_setup",
                "outcome": setup.mcp_config.get("action"),
                "mcp_config_path": setup.mcp_config.get("path"),
                "mcp_config_action": setup.mcp_config.get("action"),
                "server_name": setup.mcp_config.get("server_name"),
                "error_code": mcp_warning_code if isinstance(mcp_warning_code, str) else None,
            },
        ),
    )


def _emit_cli_error(target: Path, error: CliError, args: CliArgs, started: float) -> None:
    if not args.log_enabled:
        return
    _write_cli_event(
        target,
        LogEvent(
            event_name="cli.error",
            channel="cli",
            status="error",
            error_code=error.code,
            duration_ms=_elapsed_ms(started),
            summary=f"cipher2 {args.command} failed: {error.code}",
            payload={
                "operation": args.command,
                "outcome": "failed",
                "command_name": args.command,
                "exit_code": 1,
                "error_code": error.code,
            },
        ),
    )


def _emit_cli_status(target: Path, result: StatusCliResult, args: StatusCliArgs, started: float) -> None:
    if result.overview is None:
        return
    overview = result.overview
    _write_cli_event(
        target,
        LogEvent(
            event_name="cli.status",
            channel="cli",
            status="ok",
            duration_ms=_elapsed_ms(started),
            summary=f"cipher2 status rendered {overview.state}",
            counts={
                "section_count": 3,
                "error_count": len(overview.errors),
            },
            payload={
                "operation": "status",
                "outcome": "rendered",
                "command_name": "status",
                "json_output": args.json_output,
                "overview_state": overview.state,
            },
        ),
    )


def _emit_rebuild_events(target: Path, result: CliResult, args: CliArgs, started: float) -> None:
    if not args.log_enabled:
        return
    rebuild_payload = {
        "operation": "rebuild",
        "outcome": "written",
        "snapshot_id": result.snapshot_id,
        "profile": args.profile,
    }
    rebuild_counts = {
        "fact_count": result.fact_count,
        "relative_count": result.relative_count,
        "source_count": result.source_count,
        "warning_count": result.warning_count,
    }
    _write_cli_event(
        target,
        LogEvent(
            event_name="initializer.rebuild",
            channel="initializer",
            status="ok",
            duration_ms=_elapsed_ms(started),
            summary=f"rebuilt {result.fact_count} facts",
            counts=rebuild_counts,
            payload=rebuild_payload,
        ),
    )
    _write_cli_event(
        target,
        LogEvent(
            event_name="incremental.rebuild_published",
            channel="incremental",
            status="ok",
            duration_ms=_elapsed_ms(started),
            counts={
                "fact_count": result.fact_count,
                "relative_count": result.relative_count,
                "source_count": result.source_count,
            },
            payload={"snapshot_id": result.snapshot_id},
        ),
    )


def _write_cli_event(target: Path, event: LogEvent) -> None:
    try:
        open_log(target).write_event(event)
    except LogError:
        pass


def _root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cipher2", description="cipher-2 FACT-only command line tools", add_help=False)
    parser.add_argument("--help", "-h", action="store_true", help="show this help message and exit")
    parser.add_argument("--version", "-V", action="store_true", help="show package version and exit")
    parser.add_argument("command", nargs="?", help="subcommand: init, rebuild, or status")
    return parser


def _command_parser(command: str) -> _NoExitParser:
    if command == "init":
        description = "initialize a target repository"
    elif command == "rebuild":
        description = "rebuild a target repository snapshot"
    else:
        description = "render target repository status"
    parser = _NoExitParser(prog=f"cipher2 {command}", description=description, add_help=False)
    parser.add_argument("--help", "-h", action="store_true", help="show this help message and exit")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--json", dest="json_output", action="store_true")
    if command != "status":
        parser.add_argument("--source-root", dest="source_roots", action="append", default=[])
        parser.add_argument("--profile", default="default")
        parser.add_argument("--compile-database")
        parser.add_argument("--no-log", action="store_true")
        if command == "init":
            parser.add_argument("--no-mcp-config", action="store_true")
            parser.add_argument("--print-mcp-config", action="store_true")
            parser.add_argument("--no-progress", action="store_true")
    return parser


def _wants_root_help(argv: Sequence[str]) -> bool:
    return not argv or argv[0] in ("--help", "-h")


def _wants_command_help(argv: Sequence[str]) -> bool:
    return bool(argv) and argv[0] in COMMANDS and any(item in ("--help", "-h") for item in argv[1:])


def _render_status_human(target_repo: Path, overview: ToolsOverviewModel) -> str:
    lines = [
        f"cipher-2 status: {target_repo}",
        f"state: {overview.state}",
        "",
    ]
    lines.extend(_render_storage_section(overview))
    lines.append("")
    lines.extend(_render_log_section(overview))
    lines.append("")
    lines.extend(_render_incremental_section(overview))
    return "\n".join(lines) + "\n"


def _render_storage_section(overview: ToolsOverviewModel) -> List[str]:
    storage = overview.storage
    if storage is None:
        code = _section_error_code(overview, "storage")
        return [
            "storage: error",
            f"  error: {code}",
            "  snapshot: -",
            "  facts: -  relatives: -",
            "  field_read: -  field_write: -",
            "  sources: -  profiles: -",
        ]
    return [
        f"storage: {storage.state}",
        f"  snapshot: {_dash(storage.snapshot_id)}",
        f"  format: {_dash(storage.snapshot_format)}  compression: {_dash(storage.compression)}",
        (
            "  bytes: "
            f"{_fmt_int(storage.bytes_on_disk)} compressed / {_fmt_int(storage.uncompressed_bytes)} raw  "
            f"ratio: {storage.compression_ratio * 100:.2f}%"
        ),
        (
            "  read_index: "
            f"{storage.read_index_state}  bytes: {_fmt_int(storage.read_index_bytes)}  "
            f"schema: {_dash(storage.read_index_schema_version)}  codec: {_dash(storage.read_index_codec)}"
        ),
        f"  facts: {_fmt_int(storage.total_facts)}  relatives: {_fmt_int(storage.total_relatives)}",
        f"  field_read: {_fmt_int(storage.field_read_count)}  field_write: {_fmt_int(storage.field_write_count)}",
        f"  sources: {_fmt_int(storage.total_sources)}  profiles: {_fmt_keys(storage.profiles)}",
    ]


def _render_log_section(overview: ToolsOverviewModel) -> List[str]:
    log = overview.log
    if log is None:
        code = _section_error_code(overview, "log")
        return [
            "log: error",
            f"  error: {code}",
            "  events: -  channels: -",
            "  errors: -",
            "  latest: -",
        ]
    return [
        f"log: {log.state}",
        f"  events: {_fmt_int(log.total_events)}  channels: {_fmt_keys(log.events_by_channel)}",
        f"  init stages: {_fmt_init_stage_timings(log.init_stage_timings)}",
        (
            f"  extractor workers: mode={_dash(log.extractor_worker_mode)} "
            f"count={_fmt_int(log.extractor_worker_count)} "
            f"ok={_fmt_int(log.extractor_worker_successful_file_count)} "
            f"skipped={_fmt_int(log.extractor_worker_skipped_file_count)}"
        ),
        f"  errors: {_fmt_errors(log.error_codes)}",
        f"  latest: {_dash(log.latest_event_at)}",
    ]


def _render_incremental_section(overview: ToolsOverviewModel) -> List[str]:
    incremental = overview.incremental
    if incremental is None:
        code = _section_error_code(overview, "incremental")
        return [
            "incremental: error",
            f"  error: {code}",
            "  base: -  overlay: -",
            "  dirty: -  pending: -  failed: -",
        ]
    return [
        f"incremental: {incremental.state}",
        f"  base: {_dash(incremental.base_snapshot_id)}  overlay: {_dash(incremental.active_overlay_id)}",
        (
            f"  dirty: {_fmt_int(incremental.dirty_source_count)}  "
            f"pending: {_fmt_int(incremental.pending_task_count)}  "
            f"failed: {_fmt_int(incremental.failed_task_count)}"
        ),
    ]


def _section_error_code(overview: ToolsOverviewModel, section: str) -> str:
    for error in overview.errors:
        if error.section == section or error.section.startswith(f"{section}."):
            return error.code
    return "-"


def _dash(value: Optional[str]) -> str:
    return value if value else "-"


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_keys(values: Dict[str, int]) -> str:
    if not values:
        return "-"
    return ", ".join(sorted(values))


def _fmt_errors(values: Dict[str, int]) -> str:
    if not values:
        return "-"
    return ", ".join(f"{key}({values[key]})" for key in sorted(values))


def _fmt_init_stage_timings(stage_timings) -> str:
    if not stage_timings:
        return "-"
    return " ".join(f"{stage.stage}={_fmt_duration_ms(stage.duration_ms)}" for stage in stage_timings)


def _fmt_duration_ms(value: float) -> str:
    return f"{max(0, round(value))}ms"


def _int_count(counts: Dict[str, int], key: str) -> int:
    value = counts.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _truncate_middle(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    head = max(1, (limit - 3) // 2)
    tail = max(1, limit - 3 - head)
    return f"{value[:head]}...{value[-tail:]}"


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000)


def _is_tty(handle: TextIO) -> bool:
    isatty = getattr(handle, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


__all__ = [
    "CliArgs",
    "CliError",
    "CliResult",
    "StatusCliArgs",
    "StatusCliResult",
    "StatusRenderOptions",
    "main",
    "parse_args",
    "render_result",
    "render_status_result",
    "run_init",
    "run_status",
]

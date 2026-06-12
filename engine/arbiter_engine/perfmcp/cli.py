from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .analysis import measure_command, probe_toolchain, scan_c_project
from .mcp import serve_stdio
from .tools import list_tools


# Absorbed into arbiter-engine (ADR-0010): the standalone `init` subcommand is
# dropped — arbiter's Go deploy owns all Claude Code wiring. This CLI keeps
# the runtime surface: serve + scan + probe + measure + tools.


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return serve_stdio()

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        if args.root:
            # Pin the default project root for every tool call: stdio servers
            # cannot assume their spawn cwd (Claude Code's is host-defined),
            # and a wrong implicit root makes scans silently empty.
            os.environ["CLAUDE_PROJECT_DIR"] = str(Path(args.root).expanduser().resolve())
        return serve_stdio()
    if args.command == "scan":
        payload = scan_c_project(
            root=args.root,
            paths=args.path,
            max_findings=args.max_findings,
            min_severity=args.min_severity,
            include_tests=args.include_tests,
            include_low_confidence=args.include_low_confidence,
            budget_files=args.budget_files,
            budget_bytes=args.budget_bytes,
        )
        _print_payload(payload, args.json)
        return 0
    if args.command == "probe":
        _print_payload(probe_toolchain(args.root), args.json)
        return 0
    if args.command == "measure":
        command_argv = list(args.command_argv)
        if command_argv and command_argv[0] == "--":
            command_argv = command_argv[1:]
        if not command_argv:
            parser.error("measure requires a command after --")
        payload = measure_command(
            command=command_argv,
            cwd=args.cwd,
            repeat=args.repeat,
            timeout_seconds=args.timeout_seconds,
            env=_env_pairs(args.env),
            max_output_chars=args.max_output_chars,
        )
        _print_payload(payload, args.json)
        return 1 if payload.get("is_error") else 0
    if args.command == "tools":
        _print_payload({"schema_version": "perf-mcp.tools.v1", "tools": list_tools()}, True)
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arbiter-engine perfmcp",
        description="C performance triage MCP server, bundled with arbiter.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the stdio MCP server.")
    serve.add_argument("--root", default=None, help="Default project root for tool calls (absolute recommended).")

    scan = subparsers.add_parser("scan", help="Scan C/C++ files for performance risks.")
    scan.add_argument("root", nargs="?", default=None, help="Project root. Defaults to CLAUDE_PROJECT_DIR or cwd.")
    scan.add_argument("--path", action="append", help="Project-relative file or directory to scan. May repeat.")
    scan.add_argument("--max-findings", type=int, default=50)
    scan.add_argument("--min-severity", choices=["low", "medium", "high"], default="medium")
    scan.add_argument("--include-tests", action="store_true")
    scan.add_argument("--include-low-confidence", action="store_true")
    scan.add_argument("--budget-files", type=int, default=250)
    scan.add_argument("--budget-bytes", type=int, default=5_000_000)
    scan.add_argument("--json", action="store_true", help="Emit full JSON.")

    probe = subparsers.add_parser("probe", help="Report available local performance tooling.")
    probe.add_argument("root", nargs="?", default=None)
    probe.add_argument("--json", action="store_true", help="Emit full JSON.")

    measure = subparsers.add_parser("measure", help="Measure an argv command repeatedly.")
    measure.add_argument("--cwd", default=None)
    measure.add_argument("--repeat", type=int, default=3)
    measure.add_argument("--timeout-seconds", type=float, default=60.0)
    measure.add_argument("--env", action="append", default=[], help="Extra KEY=VALUE environment. May repeat.")
    measure.add_argument("--max-output-chars", type=int, default=20_000)
    measure.add_argument("--json", action="store_true", help="Emit full JSON.")
    measure.add_argument("command_argv", nargs=argparse.REMAINDER, help="Command after --.")

    subparsers.add_parser("tools", help="Print MCP tool definitions as JSON.")
    return parser


def _env_pairs(values: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--env must be KEY=VALUE, got {value!r}")
        key, raw = value.split("=", 1)
        env[key] = raw
    return env


def _print_payload(payload: dict[str, Any], full_json: bool) -> None:
    if full_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    schema = payload.get("schema_version")
    if schema == "perf-mcp.scan.v1":
        summary = payload.get("summary", {})
        print(f"{summary.get('finding_count', 0)} findings across {payload.get('files_scanned', 0)} files")
        for finding in payload.get("findings", [])[:20]:
            location = finding.get("location", {})
            print(
                "{id} {severity}/{confidence} {rule} {path}:{line} {title}".format(
                    id=finding.get("id"),
                    severity=finding.get("severity"),
                    confidence=finding.get("confidence"),
                    rule=finding.get("rule_id"),
                    path=location.get("path"),
                    line=location.get("line"),
                    title=finding.get("title"),
                )
            )
        for warning in payload.get("warnings", []):
            print(f"warning: {warning}", file=sys.stderr)
        return
    if schema == "perf-mcp.probe.v1":
        tools = payload.get("tools", {})
        for name, info in tools.items():
            if info.get("available"):
                print(f"{name}: {info.get('path')}")
        for recommendation in payload.get("recommendations", []):
            print(f"recommendation: {recommendation}")
        return
    if schema == "perf-mcp.measure.v1":
        print(json.dumps(payload.get("summary", {}), indent=2, sort_keys=True))
        return
    print(json.dumps(payload, indent=2, sort_keys=True))

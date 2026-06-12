from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from . import __version__
from .config import Config
from .diagnostics import doctor
from .server import serve


# Absorbed into arbiter-engine (ADR-0010): the standalone `init` subcommand is
# dropped — arbiter's Go deploy owns all Claude Code wiring. This CLI keeps
# only the runtime surface: serve + doctor.


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="arbiter-engine gdbmcp")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser("serve", help="run the stdio MCP server")
    serve_parser.add_argument("--root", default=None, help="repo/project root; defaults to cwd or GDB_MCP_ROOT")
    serve_parser.add_argument("--gdb", default=None, help="GDB executable path")
    serve_parser.add_argument("--allow-outside-root", action="store_true", help="allow target/core/source paths outside root")
    serve_parser.add_argument("--allow-attach", action="store_true", help="allow gdb_start mode=attach")
    serve_parser.add_argument("--allow-remote", action="store_true", help="allow gdb_start mode=remote")
    serve_parser.add_argument("--allow-dangerous-commands", action="store_true", help="allow dangerous gdb_command classes")
    serve_parser.add_argument("--no-audit", action="store_true", help="disable .gdb-mcp/audit.jsonl")

    doctor_parser = sub.add_parser("doctor", help="check local runtime prerequisites")
    doctor_parser.add_argument("--root", default=".", help="project root")
    doctor_parser.add_argument("--gdb", default=None, help="GDB executable path")
    doctor_parser.add_argument("--json", action="store_true", help="print JSON")

    args = parser.parse_args(argv)
    if args.version:
        print(f"arbiter-engine gdbmcp {__version__}")
        return 0
    if args.command == "serve":
        config = Config.from_env(args.root, args.gdb)
        config = _override_config(
            config,
            allow_outside_root=args.allow_outside_root,
            allow_attach=args.allow_attach,
            allow_remote=args.allow_remote,
            allow_dangerous_commands=args.allow_dangerous_commands,
            audit=not args.no_audit,
        )
        return serve(config)
    if args.command == "doctor":
        payload = doctor(Path(args.root), gdb=args.gdb)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for check in payload["checks"]:
                status = "ok" if check["ok"] else "missing"
                print(f"{status}: {check['name']} - {check['detail']}")
        return 0 if payload["ok"] else 1
    parser.print_help()
    return 2


def _override_config(
    config: Config,
    *,
    allow_outside_root: bool,
    allow_attach: bool,
    allow_remote: bool,
    allow_dangerous_commands: bool,
    audit: bool,
) -> Config:
    return Config(
        root=config.root,
        gdb_path=config.gdb_path,
        allow_outside_root=config.allow_outside_root or allow_outside_root,
        allow_attach=config.allow_attach or allow_attach,
        allow_remote=config.allow_remote or allow_remote,
        allow_dangerous_commands=config.allow_dangerous_commands or allow_dangerous_commands,
        audit=config.audit and audit,
        max_sessions=config.max_sessions,
        event_limit=config.event_limit,
        stream_limit=config.stream_limit,
    )


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from .config import Config
from .diagnostics import doctor
from .errors import ToolError
from .sessions import SessionManager


Handler = Callable[[Mapping[str, Any]], Dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Handler
    output_schema: Optional[Dict[str, Any]] = None

    def descriptor(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.output_schema:
            payload["outputSchema"] = self.output_schema
        return payload


class ToolRegistry:
    def __init__(self, manager: SessionManager, config: Config):
        self.manager = manager
        self.config = config
        self._tools: Dict[str, Tool] = {}
        for tool in _build_tools(manager, config):
            self._tools[tool.name] = tool

    def descriptors(self) -> list[Dict[str, Any]]:
        return [self._tools[name].descriptor() for name in sorted(self._tools)]

    def call(self, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("tool_not_found", "unknown tool", {"tool": name})
        return tool.handler(arguments)

    def schema(self, name: str) -> Dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("tool_not_found", "unknown tool", {"tool": name})
        return tool.input_schema


def _build_tools(manager: SessionManager, config: Config) -> list[Tool]:
    return [
        Tool(
            "gdb_start",
            "Start a bounded GDB/MI session for an executable, core file, attach target, or opt-in remote target.",
            _object(
                {
                    "mode": _enum(["exec", "attach", "core", "remote"], default="exec"),
                    "target": _string("Executable path, relative to the server root or cwd."),
                    "cwd": _string("Working directory, relative to the server root.", default="."),
                    "args": _array({"type": "string"}, "Program arguments."),
                    "env": _object({}, "Extra environment variables for GDB/inferior.", additional=True),
                    "pid": {"type": "integer", "minimum": 1},
                    "core": _string("Core file path for core mode."),
                    "remote_endpoint": _string("GDB remote endpoint for mode=remote, e.g. localhost:1234 or /dev/ttyUSB0."),
                    "session_name": _string("Optional human-readable session label."),
                    "run_until": _enum(["none", "main", "entry"], default="none"),
                    "wait_ms": {"type": "integer", "minimum": 0, "maximum": 60000, "default": 1000},
                }
            ),
            lambda args: manager.start(
                mode=str(args.get("mode", "exec")),
                target=_optional_str(args.get("target")),
                cwd=_optional_str(args.get("cwd", ".")),
                args=[str(item) for item in args.get("args", [])],
                env={str(k): str(v) for k, v in dict(args.get("env", {})).items()},
                pid=args.get("pid"),
                core=_optional_str(args.get("core")),
                remote_endpoint=_optional_str(args.get("remote_endpoint")),
                name=_optional_str(args.get("session_name")),
                run_until=str(args.get("run_until", "none")),
                wait_ms=int(args.get("wait_ms", 1000)),
            ),
            _generic_output(),
        ),
        Tool(
            "gdb_diagnostics",
            "Report server, GDB, root, and local inferior run readiness as structured checks.",
            _object({}),
            lambda args: doctor(config.root, gdb=config.gdb_path),
            _generic_output(),
        ),
        Tool(
            "gdb_exec",
            "Control inferior execution: run, continue, next, step, finish, until, interrupt, or wait.",
            _object(
                {
                    "session_id": _string("Session id returned by gdb_start."),
                    "action": _enum(["run", "continue", "next", "step", "finish", "until", "interrupt", "wait"]),
                    "location": _string("Location for action=until."),
                    "count": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
                    "wait_ms": {"type": "integer", "minimum": 0, "maximum": 60000, "default": 1000},
                },
                required=("session_id", "action"),
            ),
            lambda args: manager.get(str(args["session_id"])).run_control(
                str(args["action"]),
                location=_optional_str(args.get("location")),
                count=int(args.get("count", 1)),
                wait_ms=int(args.get("wait_ms", 1000)),
            ),
            _generic_output(),
        ),
        Tool(
            "gdb_breakpoint",
            "Set, list, delete, enable, disable, or clear GDB breakpoints and watchpoints.",
            _object(
                {
                    "session_id": _string("Session id returned by gdb_start."),
                    "action": _enum(["set", "list", "delete", "enable", "disable", "clear_all"]),
                    "kind": _enum(["breakpoint", "watch", "rwatch", "awatch"], default="breakpoint"),
                    "location": _string("Breakpoint location or watch expression, e.g. main, file.c:42, *0x401000, or variable_name."),
                    "breakpoint_id": _string("GDB breakpoint number."),
                    "temporary": {"type": "boolean", "default": False},
                    "condition": _string("Optional GDB breakpoint condition."),
                    "ignore_count": {"type": "integer", "minimum": 0, "maximum": 1000000},
                    "hardware": {"type": "boolean", "default": False},
                },
                required=("session_id", "action"),
            ),
            lambda args: _breakpoint(manager, args),
            _generic_output(),
        ),
        Tool(
            "gdb_stack",
            "Return a bounded backtrace and optional source context for the selected frame.",
            _object(
                {
                    "session_id": _string("Session id returned by gdb_start."),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                    "include_source": {"type": "boolean", "default": False},
                    "source_radius": {"type": "integer", "minimum": 0, "maximum": 20, "default": 4},
                },
                required=("session_id",),
            ),
            lambda args: _with_session(
                manager,
                args,
                lambda s: {
                    "ok": True,
                    "session_id": s.session_id,
                    **s.stack(
                        limit=int(args.get("limit", 20)),
                        include_source=bool(args.get("include_source", False)),
                        source_radius=int(args.get("source_radius", 4)),
                    ),
                },
            ),
            _generic_output(),
        ),
        Tool(
            "gdb_snapshot",
            "Collect stop reason, threads, stack, locals, args, and optionally registers in one bounded bundle.",
            _object(
                {
                    "session_id": _string("Session id returned by gdb_start."),
                    "stack_limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 12},
                    "include_registers": {"type": "boolean", "default": True},
                    "include_source": {"type": "boolean", "default": True},
                },
                required=("session_id",),
            ),
            lambda args: _with_session(
                manager,
                args,
                lambda s: s.snapshot(
                    stack_limit=int(args.get("stack_limit", 12)),
                    include_registers=bool(args.get("include_registers", True)),
                    include_source=bool(args.get("include_source", True)),
                ),
            ),
            _generic_output(),
        ),
        Tool(
            "gdb_eval",
            "Evaluate an expression, print a type, or list locals, args, registers, or threads.",
            _object(
                {
                    "session_id": _string("Session id returned by gdb_start."),
                    "mode": _enum(["expression", "type", "locals", "args", "registers", "threads"], default="expression"),
                    "expression": _string("C/C++ expression for mode=expression or mode=type."),
                    "register_format": _enum(["x", "d", "N", "r"], default="x"),
                },
                required=("session_id",),
            ),
            lambda args: _eval(manager, args),
            _generic_output(),
        ),
        Tool(
            "gdb_memory",
            "Read bounded inferior memory bytes at an address or expression.",
            _object(
                {
                    "session_id": _string("Session id returned by gdb_start."),
                    "address": _string("Address or expression accepted by GDB, e.g. 0x1000 or &var."),
                    "count": {"type": "integer", "minimum": 1, "maximum": 4096, "default": 64},
                },
                required=("session_id", "address"),
            ),
            lambda args: _with_session(
                manager,
                args,
                lambda s: {"ok": True, "session_id": s.session_id, **s.memory(str(args["address"]), count=int(args.get("count", 64)))},
            ),
            _generic_output(),
        ),
        Tool(
            "gdb_command",
            "Run a guarded GDB console command when the structured tools are insufficient.",
            _object(
                {
                    "session_id": _string("Session id returned by gdb_start."),
                    "command": _string("GDB console command. Dangerous commands are denied unless the server opts in."),
                    "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60000, "default": 10000},
                },
                required=("session_id", "command"),
            ),
            lambda args: _command(manager, config, args),
            _generic_output(),
        ),
        Tool(
            "gdb_sessions",
            "List active in-memory GDB sessions.",
            _object({"include_events": {"type": "boolean", "default": False}}),
            lambda args: _sessions(manager, bool(args.get("include_events", False))),
            _generic_output(),
        ),
        Tool(
            "gdb_select",
            "Select the active GDB thread and/or stack frame for later eval, stack, and snapshot calls.",
            _object(
                {
                    "session_id": _string("Session id returned by gdb_start."),
                    "thread_id": _string("GDB thread id to select."),
                    "frame_level": {"type": "integer", "minimum": 0, "maximum": 10000},
                },
                required=("session_id",),
            ),
            lambda args: _select(manager, args),
            _generic_output(),
        ),
        Tool(
            "gdb_stop",
            "Terminate one session, or all sessions when all=true.",
            _object(
                {
                    "session_id": _string("Session id returned by gdb_start."),
                    "all": {"type": "boolean", "default": False},
                }
            ),
            lambda args: manager.stop(None if bool(args.get("all", False)) else _require_session_id(args)),
            _generic_output(),
        ),
    ]


def _breakpoint(manager: SessionManager, args: Mapping[str, Any]) -> Dict[str, Any]:
    session = manager.get(str(args["session_id"]))
    action = str(args["action"])
    if action == "set":
        location = _optional_str(args.get("location"))
        if not location:
            raise ToolError("bad_arguments", "location is required for action=set")
        result = session.set_breakpoint(
            location,
            kind=str(args.get("kind", "breakpoint")),
            temporary=bool(args.get("temporary", False)),
            condition=_optional_str(args.get("condition")),
            ignore_count=args.get("ignore_count"),
            hardware=bool(args.get("hardware", False)),
        )
        return {"ok": True, "session_id": session.session_id, **result}
    return {"ok": True, "session_id": session.session_id, **session.breakpoint_action(action, _optional_str(args.get("breakpoint_id")))}


def _eval(manager: SessionManager, args: Mapping[str, Any]) -> Dict[str, Any]:
    session = manager.get(str(args["session_id"]))
    mode = str(args.get("mode", "expression"))
    if mode == "expression":
        expression = _optional_str(args.get("expression"))
        if not expression:
            raise ToolError("bad_arguments", "expression is required for mode=expression")
        return {"ok": True, "session_id": session.session_id, **session.eval_expression(expression)}
    if mode == "type":
        expression = _optional_str(args.get("expression"))
        if not expression:
            raise ToolError("bad_arguments", "expression is required for mode=type")
        return {"ok": True, "session_id": session.session_id, **session.eval_type(expression)}
    if mode == "locals":
        return {"ok": True, "session_id": session.session_id, **session.locals()}
    if mode == "args":
        return {"ok": True, "session_id": session.session_id, **session.args_info()}
    if mode == "registers":
        return {"ok": True, "session_id": session.session_id, **session.registers(fmt=str(args.get("register_format", "x")))}
    if mode == "threads":
        return {"ok": True, "session_id": session.session_id, **session.threads()}
    raise ToolError("bad_arguments", f"unknown eval mode: {mode}")


def _command(manager: SessionManager, config: Config, args: Mapping[str, Any]) -> Dict[str, Any]:
    session = manager.get(str(args["session_id"]))
    command = str(args["command"]).strip()
    if not command:
        raise ToolError("bad_arguments", "command must not be empty")
    if _has_control_chars(command):
        raise ToolError(
            "dangerous_command_denied",
            "GDB console command must be a single line without control characters",
            {"command_class": "multi-statement", "hint": "issue one console command per call"},
        )
    danger = _dangerous_command(command)
    if danger and not config.allow_dangerous_commands:
        raise ToolError(
            "dangerous_command_denied",
            "GDB command is disabled by default",
            {"command_class": danger, "hint": "restart gdb-mcp with --allow-dangerous-commands if this is intentional"},
        )
    result = session.console(command, timeout_ms=int(args.get("timeout_ms", 10000)))
    text = "\n".join(result.streams).strip()
    return {
        "ok": True,
        "session_id": session.session_id,
        "command": command.split(" ", 1)[0],
        "output": text,
        "mi": result.to_json(),
    }


def _sessions(manager: SessionManager, include_events: bool) -> Dict[str, Any]:
    sessions = manager.list()
    if include_events:
        for item in sessions:
            sid = item.get("session_id")
            if isinstance(sid, str):
                item["events"] = manager.get(sid).recent_events(20)
    return {"ok": True, "sessions": sessions, "count": len(sessions)}


def _select(manager: SessionManager, args: Mapping[str, Any]) -> Dict[str, Any]:
    thread_id = _optional_str(args.get("thread_id"))
    frame_level = args.get("frame_level")
    if thread_id is None and frame_level is None:
        raise ToolError("bad_arguments", "thread_id or frame_level is required")
    session = manager.get(str(args["session_id"]))
    return {
        "ok": True,
        "session_id": session.session_id,
        **session.select(thread_id=thread_id, frame_level=frame_level),
    }


def _with_session(manager: SessionManager, args: Mapping[str, Any], fn: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    return fn(manager.get(str(args["session_id"])))


def _require_session_id(args: Mapping[str, Any]) -> str:
    session_id = _optional_str(args.get("session_id"))
    if not session_id:
        raise ToolError("bad_arguments", "session_id is required unless all=true")
    return session_id


_DANGEROUS_KEYWORDS = {"shell", "source", "python", "pi", "guile", "compile", "dump", "restore", "maintenance"}
# Unicode line/paragraph separators that str.split() treats as whitespace and
# that a console parser could treat as a line break, but which fall outside the
# ASCII control-char range (NEL, LINE/PARAGRAPH SEPARATOR).
_UNICODE_LINE_SEPARATORS = "\u0085\u2028\u2029"
# Characters that begin a fresh statement on one line; splitting on these (not
# just whitespace) stops a denied keyword from hiding glued to a separator,
# e.g. "print 1;shell id" → token "1;shell" would otherwise evade the scan.
_TOKEN_SEPARATORS = re.compile(r"[\s;|&]+")


def _has_control_chars(command: str) -> bool:
    # Newlines/carriage returns/other control chars let a denied keyword hide
    # past the first token (e.g. "print 1\nshell id"); GDB may treat the
    # embedded separator as a fresh command, evading a first-token-only gate.
    return any(ord(char) < 0x20 or ord(char) == 0x7F or char in _UNICODE_LINE_SEPARATORS for char in command)


def _dangerous_command(command: str) -> Optional[str]:
    lowered = command.strip().lower()
    # Scan EVERY separator-delimited token, not just the first, so a denied
    # keyword anywhere in the command is caught even if a control-char
    # pre-check were ever relaxed. Split on inline separators (;|&) as well as
    # whitespace so a keyword glued to one is still isolated.
    tokens = set(_TOKEN_SEPARATORS.split(lowered))
    match = _DANGEROUS_KEYWORDS.intersection(tokens)
    if match:
        return sorted(match)[0]
    if lowered.startswith("target remote") or lowered.startswith("target extended-remote"):
        return "target-remote"
    if lowered.startswith("generate-core-file") or lowered.startswith("gcore"):
        return "core-write"
    if lowered.startswith("set logging") or lowered.startswith("set exec-wrapper") or lowered.startswith("set inferior-tty"):
        return "host-io"
    return None


def summarize_call(name: str, args: Mapping[str, Any], result: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"tool": name}
    if "session_id" in args:
        summary["session_id"] = args.get("session_id")
    if name == "gdb_start":
        summary.update(
            {
                "mode": args.get("mode", "exec"),
                "target": args.get("target"),
                "arg_count": len(args.get("args", []) if isinstance(args.get("args", []), list) else []),
                "env": args.get("env", {}),
            }
        )
    elif name == "gdb_command":
        command = str(args.get("command", ""))
        summary["command_class"] = command.strip().split(" ", 1)[0] if command.strip() else ""
        summary["command_length"] = len(command)
    else:
        for key in ("action", "mode", "location", "count", "limit", "address"):
            if key in args:
                summary[key] = args[key]
    if result is not None:
        summary["state"] = result.get("state")
        summary["ok"] = result.get("ok")
    return summary


def text_summary(payload: Mapping[str, Any]) -> str:
    if not payload.get("ok", False):
        error = payload.get("error", {})
        if isinstance(error, Mapping):
            return f"{error.get('code', 'error')}: {error.get('message', 'tool failed')}"
        return "tool failed"
    parts = ["ok"]
    session_id = payload.get("session_id")
    if session_id:
        parts.append(f"session={session_id}")
    state = payload.get("state")
    if state:
        parts.append(f"state={state}")
    if "count" in payload:
        parts.append(f"count={payload['count']}")
    if "frames" in payload and isinstance(payload["frames"], list):
        parts.append(f"frames={len(payload['frames'])}")
    if "breakpoints" in payload and isinstance(payload["breakpoints"], list):
        parts.append(f"breakpoints={len(payload['breakpoints'])}")
    if "value" in payload:
        parts.append(f"value={payload['value']}")
    if "output" in payload and payload["output"]:
        text = str(payload["output"]).replace("\n", " ")
        parts.append(text[:160])
    return " ".join(parts)


def _object(
    properties: Mapping[str, Mapping[str, Any]],
    description: Optional[str] = None,
    *,
    required: tuple[str, ...] = (),
    additional: bool = False,
) -> Dict[str, Any]:
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": {key: dict(value) for key, value in properties.items()},
        "additionalProperties": additional,
    }
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


def _array(items: Mapping[str, Any], description: str) -> Dict[str, Any]:
    return {"type": "array", "items": dict(items), "description": description, "default": []}


def _string(description: str, default: Optional[str] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "string", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def _enum(values: list[str], default: Optional[str] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "string", "enum": values}
    if default is not None:
        schema["default"] = default
    return schema


def _generic_output() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "session_id": {"type": "string"},
            "state": {"type": "string"},
        },
        "required": ["ok"],
        "additionalProperties": True,
    }


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text else None

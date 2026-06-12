from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional

from . import mi
from .audit import AuditLog
from .config import Config
from .errors import ToolError


RUNNING_CLASSES = {"running", "connected"}
STOP_CLASSES = {"stopped"}


@dataclass
class CommandResult:
    record: mi.MIRecord
    events: List[Dict[str, Any]]
    streams: List[str]
    stopped: Optional[Dict[str, Any]] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "record": self.record.to_json(),
            "events": self.events,
            "streams": self.streams,
            "stopped": self.stopped,
        }


class GDBSession:
    def __init__(
        self,
        *,
        config: Config,
        session_id: str,
        name: Optional[str],
        cwd: Path,
        target: Optional[Path],
        mode: str,
        argv: List[str],
        env: Mapping[str, str],
    ):
        self.config = config
        self.session_id = session_id
        self.name = name
        self.cwd = cwd
        self.target = target
        self.mode = mode
        self.argv = list(argv)
        self.extra_env = dict(env)
        self.created_at = time.time()
        self.state = "starting"
        self.last_stop: Optional[Dict[str, Any]] = None
        self.inferior_pid: Optional[int] = None
        self._token = 0
        self._lock = threading.RLock()
        self._waiters: Dict[int, "queue.Queue[mi.MIRecord]"] = {}
        self._events: Deque[Dict[str, Any]] = deque(maxlen=config.event_limit)
        self._streams: Deque[str] = deque(maxlen=config.event_limit)
        self._event_seq = 0
        self._closed = False
        self._proc = self._spawn_gdb()
        self._reader = threading.Thread(target=self._reader_loop, name=f"gdb-mcp-reader-{session_id}", daemon=True)
        self._reader.start()
        try:
            self._bootstrap()
        except Exception:
            self.close()
            raise

    def _spawn_gdb(self) -> subprocess.Popen:
        env = os.environ.copy()
        env.update(self.extra_env)
        try:
            return subprocess.Popen(
                [self.config.gdb_executable(), "--interpreter=mi3", "-q", "--nx", "--nh"],
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError("gdb_spawn_failed", str(exc))

    def _bootstrap(self) -> None:
        self.state = "stopped"
        for command in (
            "-gdb-set mi-async on",
            "-gdb-set pagination off",
            "-gdb-set confirm off",
            "-gdb-set print pretty on",
            "-gdb-set breakpoint pending on",
        ):
            self.command(command, timeout_ms=5000)
        if self.cwd:
            self.command(f"-environment-cd {mi.quote(str(self.cwd))}", timeout_ms=5000)
        if self.target is not None:
            self.command(f"-file-exec-and-symbols {mi.quote(str(self.target))}", timeout_ms=10000)
        if self.argv:
            args = " ".join(mi.quote(arg) for arg in self.argv)
            self.command(f"-exec-arguments {args}", timeout_ms=5000)

    def _reader_loop(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            try:
                record = mi.parse_line(line)
            except Exception as exc:
                self._append_event({"kind": "parse_error", "message": str(exc), "raw": line.rstrip("\n")})
                continue
            if record is None:
                continue
            if record.kind == "result" and record.token is not None:
                self._apply_record(record)
                waiter = self._waiters.get(record.token)
                if waiter is not None:
                    waiter.put(record)
            elif record.kind in {"exec", "status", "notify"}:
                self._apply_record(record)
                self._append_event(record.to_json())
            elif record.kind in {"console", "target", "log"} and record.text:
                self._streams.append(_cap_text(record.text, self.config.stream_limit))
                self._append_event(record.to_json())
        self.state = "exited"
        self._closed = True

    def _apply_record(self, record: mi.MIRecord) -> None:
        if record.kind == "result":
            if record.cls in RUNNING_CLASSES:
                self.state = "running"
            elif record.cls == "exit":
                self.state = "exited"
            elif record.cls == "error":
                self.state = "error"
        elif record.kind == "exec" and record.cls in STOP_CLASSES:
            self.state = "stopped"
            self.last_stop = normalize_stop(record.results)
            self.inferior_pid = _maybe_int(record.results.get("pid")) or self.inferior_pid
        elif record.kind == "exec" and record.cls in RUNNING_CLASSES:
            self.state = "running"
        elif record.kind == "notify" and record.cls == "thread-group-exited":
            self.state = "exited"

    def _append_event(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        self._event_seq += 1
        event["seq"] = self._event_seq
        event["ts"] = round(time.time(), 6)
        self._events.append(event)

    def command(self, command: str, *, timeout_ms: int = 10000, wait_ms: int = 0) -> CommandResult:
        if self._closed or self._proc.poll() is not None:
            raise ToolError("session_exited", "GDB session has exited", {"session_id": self.session_id})
        with self._lock:
            self._token += 1
            token = self._token
            waiter: "queue.Queue[mi.MIRecord]" = queue.Queue(maxsize=1)
            self._waiters[token] = waiter
            event_start = len(self._events)
            stream_start = len(self._streams)
            payload = f"{token}{command}\n"
            assert self._proc.stdin is not None
            try:
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()
            except OSError as exc:
                self._waiters.pop(token, None)
                raise ToolError("gdb_write_failed", str(exc), {"session_id": self.session_id})
            try:
                record = waiter.get(timeout=max(timeout_ms, 1) / 1000.0)
            except queue.Empty:
                self._waiters.pop(token, None)
                raise ToolError("gdb_timeout", "timed out waiting for GDB result", {"command": _command_name(command)})
            finally:
                self._waiters.pop(token, None)
            if record.cls == "error":
                message = str(record.results.get("msg") or record.results.get("message") or "GDB command failed")
                details: Dict[str, Any] = {"command": _command_name(command), "result": record.results}
                guidance = classify_gdb_failure(message)
                if guidance:
                    details["guidance"] = guidance
                raise ToolError("gdb_error", message, details)
            stopped = self.wait_for_stop(wait_ms) if wait_ms > 0 else None
            return CommandResult(
                record=record,
                events=list(self._slice_events(event_start)),
                streams=list(self._slice_streams(stream_start)),
                stopped=stopped,
            )

    def wait_for_stop(self, wait_ms: int) -> Optional[Dict[str, Any]]:
        if wait_ms <= 0:
            return None
        deadline = time.time() + wait_ms / 1000.0
        while time.time() < deadline:
            if self.state == "stopped" and self.last_stop is not None:
                return self.last_stop
            if self.state in {"exited", "error"}:
                return self.last_stop or {"reason": self.state}
            time.sleep(0.02)
        if self.state == "stopped":
            return self.last_stop
        return None

    def run_control(self, action: str, *, location: Optional[str] = None, count: int = 1, wait_ms: int = 1000) -> Dict[str, Any]:
        if action == "wait":
            stopped = self.wait_for_stop(wait_ms)
            return self.status_payload(extra={"stopped": stopped, "timed_out": stopped is None and self.state == "running"})
        if action == "interrupt":
            result = self.command("-exec-interrupt --all", timeout_ms=5000, wait_ms=wait_ms)
            return self.status_payload(command_result=result)
        commands = {
            "run": "-exec-run",
            "continue": "-exec-continue",
            "next": "-exec-next",
            "step": "-exec-step",
            "finish": "-exec-finish",
        }
        if action == "until":
            if not location:
                raise ToolError("bad_arguments", "location is required for until")
            command = f"-exec-until {mi.quote(location)}"
        else:
            command = commands.get(action)
            if command is None:
                raise ToolError("bad_arguments", f"unknown action: {action}")
        result: Optional[CommandResult] = None
        for _ in range(max(count, 1)):
            result = self.command(command, timeout_ms=10000, wait_ms=wait_ms)
            if self.state != "stopped":
                break
        return self.status_payload(command_result=result)

    def set_breakpoint(
        self,
        location: str,
        *,
        kind: str = "breakpoint",
        temporary: bool = False,
        condition: Optional[str] = None,
        ignore_count: Optional[int] = None,
        hardware: bool = False,
    ) -> Dict[str, Any]:
        if kind in {"watch", "rwatch", "awatch"}:
            parts = ["-break-watch"]
            if kind == "rwatch":
                parts.append("-r")
            elif kind == "awatch":
                parts.append("-a")
            parts.append(mi.quote(location))
            result = self.command(" ".join(parts), timeout_ms=10000)
            return {
                "breakpoint": _unwrap_breakpoint(
                    result.record.results.get("wpt")
                    or result.record.results.get("hw-awpt")
                    or result.record.results.get("hw-rwpt")
                    or result.record.results.get("bkpt")
                ),
                "command": result.to_json(),
            }
        if kind != "breakpoint":
            raise ToolError("bad_arguments", f"unknown breakpoint kind: {kind}")
        parts = ["-break-insert"]
        if temporary:
            parts.append("-t")
        if hardware:
            parts.append("-h")
        if condition:
            parts.extend(["-c", mi.quote(condition)])
        if ignore_count is not None:
            parts.extend(["-i", str(ignore_count)])
        parts.append(mi.quote(location))
        result = self.command(" ".join(parts), timeout_ms=10000)
        return {
            "breakpoint": _unwrap_breakpoint(result.record.results.get("bkpt")),
            "command": result.to_json(),
        }

    def breakpoint_action(self, action: str, breakpoint_id: Optional[str] = None) -> Dict[str, Any]:
        if action == "list":
            return {"breakpoints": self.list_breakpoints()}
        if action == "clear_all":
            breakpoints = self.list_breakpoints()
            ids = [str(item.get("number")) for item in breakpoints if item.get("number") is not None]
            if ids:
                self.command("-break-delete " + " ".join(ids), timeout_ms=10000)
            return {"deleted": ids, "breakpoints": []}
        if not breakpoint_id:
            raise ToolError("bad_arguments", f"breakpoint_id is required for {action}")
        commands = {
            "delete": "-break-delete",
            "enable": "-break-enable",
            "disable": "-break-disable",
        }
        command = commands.get(action)
        if command is None:
            raise ToolError("bad_arguments", f"unknown breakpoint action: {action}")
        result = self.command(f"{command} {breakpoint_id}", timeout_ms=10000)
        return {"breakpoint_id": breakpoint_id, "command": result.to_json(), "breakpoints": self.list_breakpoints()}

    def list_breakpoints(self) -> List[Dict[str, Any]]:
        result = self.command("-break-list", timeout_ms=10000)
        table = result.record.results.get("BreakpointTable") or {}
        body = table.get("body") if isinstance(table, dict) else None
        breakpoints: List[Dict[str, Any]] = []
        for item in _ensure_list(body):
            if isinstance(item, dict):
                breakpoints.append(_unwrap_breakpoint(item.get("bkpt", item)))
        return breakpoints

    def stack(self, *, limit: int = 20, include_source: bool = False, source_radius: int = 4) -> Dict[str, Any]:
        upper = max(0, limit - 1)
        result = self.command(f"-stack-list-frames 0 {upper}", timeout_ms=10000)
        frames = _frames_from_result(result.record.results.get("stack"))
        payload: Dict[str, Any] = {"frames": frames, "state": self.state, "stop": self.last_stop}
        if include_source and frames:
            payload["source"] = self.source_context(frames[0], radius=source_radius)
        return payload

    def locals(self) -> Dict[str, Any]:
        result = self.command("-stack-list-locals --simple-values", timeout_ms=10000)
        return {"locals": _named_values(result.record.results.get("locals"))}

    def args_info(self) -> Dict[str, Any]:
        result = self.command("-stack-list-arguments --simple-values 0 0", timeout_ms=10000)
        frames = result.record.results.get("stack-args") or result.record.results.get("stack_args")
        args: List[Dict[str, Any]] = []
        for item in _ensure_list(frames):
            frame = item.get("frame") if isinstance(item, dict) else item
            if isinstance(frame, dict):
                args.extend(_named_values(frame.get("args")))
        return {"args": args}

    def threads(self) -> Dict[str, Any]:
        result = self.command("-thread-info", timeout_ms=10000)
        threads = []
        for item in _ensure_list(result.record.results.get("threads")):
            if isinstance(item, dict):
                threads.append(item)
        return {"threads": threads, "current_thread_id": result.record.results.get("current-thread-id")}

    def select(self, *, thread_id: Optional[str] = None, frame_level: Optional[int] = None) -> Dict[str, Any]:
        selected: Dict[str, Any] = {}
        if thread_id is not None:
            result = self.command(f"-thread-select {thread_id}", timeout_ms=10000)
            selected["thread"] = result.record.results.get("new-thread-id") or thread_id
            selected["thread_frame"] = normalize_frame(result.record.results["frame"]) if isinstance(result.record.results.get("frame"), dict) else None
        if frame_level is not None:
            self.command(f"-stack-select-frame {frame_level}", timeout_ms=10000)
            selected["frame_level"] = frame_level
        stack = self.stack(limit=1, include_source=False)
        selected["current_frame"] = stack["frames"][0] if stack.get("frames") else None
        selected["state"] = self.state
        return selected

    def registers(self, *, fmt: str = "x") -> Dict[str, Any]:
        result = self.command(f"-data-list-register-values {fmt}", timeout_ms=10000)
        return {"registers": _named_values(result.record.results.get("register-values"))}

    def eval_expression(self, expression: str) -> Dict[str, Any]:
        result = self.command(f"-data-evaluate-expression {mi.quote(expression)}", timeout_ms=10000)
        return {"expression": expression, "value": result.record.results.get("value")}

    def eval_type(self, expression: str) -> Dict[str, Any]:
        command = f"ptype {expression}"
        result = self.console(command, timeout_ms=10000)
        return {"expression": expression, "type": "\n".join(result.streams).strip(), "command": result.to_json()}

    def memory(self, address: str, *, count: int) -> Dict[str, Any]:
        result = self.command(f"-data-read-memory-bytes {mi.quote(address)} {count}", timeout_ms=10000)
        memory = result.record.results.get("memory")
        chunks = []
        for item in _ensure_list(memory):
            if isinstance(item, dict):
                chunks.append(item)
        return {"address": address, "count": count, "memory": chunks}

    def console(self, command: str, *, timeout_ms: int = 10000) -> CommandResult:
        return self.command(f"-interpreter-exec console {mi.quote(command)}", timeout_ms=timeout_ms)

    def source_context(self, frame: Optional[Mapping[str, Any]] = None, *, radius: int = 4) -> Optional[Dict[str, Any]]:
        frame = frame or (self.last_stop or {}).get("frame")
        if not isinstance(frame, Mapping):
            return None
        fullname = frame.get("fullname") or frame.get("file")
        line = _maybe_int(frame.get("line"))
        if not fullname or line is None:
            return None
        try:
            path = self.config.resolve_existing_file(str(fullname), base=self.cwd, field="source")
        except ToolError as exc:
            return {"path": Path(str(fullname)).name, "available": False, "error": exc.code}
        start = max(1, line - radius)
        end = line + radius
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return {"path": self.config.relative(path), "available": False, "error": str(exc)}
        selected = [
            {"line": idx + 1, "text": lines[idx], "current": idx + 1 == line}
            for idx in range(start - 1, min(end, len(lines)))
        ]
        return {"path": self.config.relative(path), "available": True, "line": line, "lines": selected}

    def snapshot(self, *, stack_limit: int = 12, include_registers: bool = True, include_source: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = self.status_payload()
        try:
            payload.update(self.threads())
        except ToolError as exc:
            payload["threads_error"] = exc.to_payload()["error"]
        try:
            payload["stack"] = self.stack(limit=stack_limit, include_source=include_source)
        except ToolError as exc:
            payload["stack_error"] = exc.to_payload()["error"]
        try:
            payload.update(self.locals())
        except ToolError as exc:
            payload["locals_error"] = exc.to_payload()["error"]
        try:
            payload.update(self.args_info())
        except ToolError as exc:
            payload["args_error"] = exc.to_payload()["error"]
        if include_registers:
            try:
                payload.update(self.registers())
            except ToolError as exc:
                payload["registers_error"] = exc.to_payload()["error"]
        return payload

    def status_payload(
        self,
        *,
        command_result: Optional[CommandResult] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": True,
            "session_id": self.session_id,
            "name": self.name,
            "state": self.state,
            "mode": self.mode,
            "target": self.config.relative(self.target),
            "cwd": self.config.relative(self.cwd),
            "inferior_pid": self.inferior_pid,
            "last_stop": self.last_stop,
            "created_at": self.created_at,
            "event_count": len(self._events),
        }
        if command_result is not None:
            payload["command"] = command_result.to_json()
        if extra:
            payload.update(extra)
        return payload

    def recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        return list(self._events)[-limit:]

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._proc.poll() is None:
                try:
                    self.command("-gdb-exit", timeout_ms=1000)
                except Exception:
                    pass
            self._closed = True
            if self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            try:
                self._proc.wait(timeout=2)
            except Exception:
                pass
        finally:
            self._closed = True
            for stream in (self._proc.stdin, self._proc.stdout):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass

    def _slice_events(self, start: int) -> Iterable[Dict[str, Any]]:
        events = list(self._events)
        return events[start:] if start < len(events) else []

    def _slice_streams(self, start: int) -> Iterable[str]:
        streams = list(self._streams)
        return streams[start:] if start < len(streams) else []


class SessionManager:
    def __init__(self, config: Config, audit: Optional[AuditLog] = None):
        self.config = config
        self.audit = audit or AuditLog(config)
        self._sessions: Dict[str, GDBSession] = {}
        self._lock = threading.RLock()

    def start(
        self,
        *,
        mode: str = "exec",
        target: Optional[str] = None,
        cwd: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Mapping[str, str]] = None,
        pid: Optional[int] = None,
        core: Optional[str] = None,
        remote_endpoint: Optional[str] = None,
        name: Optional[str] = None,
        run_until: str = "none",
        wait_ms: int = 1000,
    ) -> Dict[str, Any]:
        with self._lock:
            if len(self._sessions) >= self.config.max_sessions:
                raise ToolError("too_many_sessions", "maximum GDB session count reached")
            base = self.config.root
            cwd_path = self.config.resolve_existing_dir(cwd or ".", base=base, field="cwd")
            target_path = self.config.resolve_existing_file(target, base=cwd_path, field="target") if target else None
            if mode == "attach" and not self.config.allow_attach:
                raise ToolError("attach_disabled", "attach mode requires --allow-attach")
            if mode == "remote" and not self.config.allow_remote:
                raise ToolError("remote_disabled", "remote mode requires --allow-remote")
            if mode == "core":
                if not core:
                    raise ToolError("bad_arguments", "core is required for core mode")
                self.config.resolve_existing_file(core, base=cwd_path, field="core")
            if mode == "attach" and pid is None:
                raise ToolError("bad_arguments", "pid is required for attach mode")
            if mode == "remote" and not remote_endpoint:
                raise ToolError("bad_arguments", "remote_endpoint is required for remote mode")
            session_id = uuid.uuid4().hex[:12]
            session = GDBSession(
                config=self.config,
                session_id=session_id,
                name=name,
                cwd=cwd_path,
                target=target_path,
                mode=mode,
                argv=list(args or []),
                env=env or {},
            )
            self._sessions[session_id] = session
        try:
            if mode == "attach":
                session.command(f"-target-attach {pid}", timeout_ms=15000, wait_ms=wait_ms)
            elif mode == "core":
                assert core is not None
                core_path = self.config.resolve_existing_file(core, base=cwd_path, field="core")
                session.command(f"-target-select core {mi.quote(str(core_path))}", timeout_ms=15000, wait_ms=wait_ms)
            elif mode == "remote":
                assert remote_endpoint is not None
                session.command(f"-target-select remote {mi.quote(remote_endpoint)}", timeout_ms=15000, wait_ms=wait_ms)
            elif mode != "exec":
                raise ToolError("bad_arguments", f"unknown mode: {mode}")
            if run_until == "main":
                session.set_breakpoint("main", temporary=True)
                session.run_control("run", wait_ms=wait_ms)
            elif run_until == "entry":
                session.command("-exec-run --start", timeout_ms=10000, wait_ms=wait_ms)
            elif run_until != "none":
                raise ToolError("bad_arguments", f"unknown run_until: {run_until}")
            self.audit.record(
                "finished",
                tool="gdb_start",
                session_id=session_id,
                ok=True,
                summary={"mode": mode, "target": self.config.relative(target_path), "arg_count": len(args or [])},
            )
            return session.status_payload(extra={"events": session.recent_events(10)})
        except Exception:
            with self._lock:
                self._sessions.pop(session_id, None)
            session.close()
            raise

    def get(self, session_id: str) -> GDBSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ToolError("session_not_found", "unknown GDB session", {"session_id": session_id})
        return session

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [session.status_payload() for session in sessions]

    def stop(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        stopped: List[str] = []
        with self._lock:
            if session_id is None:
                items = list(self._sessions.items())
                self._sessions.clear()
            else:
                session = self._sessions.pop(session_id, None)
                if session is None:
                    raise ToolError("session_not_found", "unknown GDB session", {"session_id": session_id})
                items = [(session_id, session)]
        for sid, session in items:
            session.close()
            stopped.append(sid)
            self.audit.record("finished", tool="gdb_stop", session_id=sid, ok=True)
        return {"ok": True, "stopped": stopped}

    def close_all(self) -> None:
        try:
            self.stop(None)
        except Exception:
            pass


def normalize_stop(results: Mapping[str, Any]) -> Dict[str, Any]:
    payload = dict(results)
    frame = payload.get("frame")
    if isinstance(frame, dict):
        payload["frame"] = normalize_frame(frame)
    return payload


def classify_gdb_failure(message: str) -> Optional[Dict[str, str]]:
    text = message.lower()
    if "don't know how to run" in text:
        return {
            "kind": "gdb_cannot_run_local_inferior",
            "summary": "This GDB build cannot launch local inferiors on this host.",
            "next_step": "Use a GDB build with native inferior support, run against a remote target that this GDB supports, or use another host for live validation.",
        }
    if "unable to find mach task port" in text or "please check gdb is codesigned" in text:
        return {
            "kind": "darwin_gdb_codesign_required",
            "summary": "macOS denied GDB debugger privileges.",
            "next_step": "Codesign GDB and grant debugger permissions before using live run or attach mode.",
        }
    if "dw_form_gnu_str_index" in text or "dw_form_strx" in text or "debug_str" in text:
        return {
            "kind": "debug_info_format_unsupported",
            "summary": "GDB could not read this binary's debug information format.",
            "next_step": "Rebuild with GDB-readable debug info, commonly -g -gdwarf-4 -O0 on macOS/Clang.",
        }
    return None


def normalize_frame(frame: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(frame)
    if "line" in out:
        line = _maybe_int(out["line"])
        if line is not None:
            out["line"] = line
    if "level" in out:
        level = _maybe_int(out["level"])
        if level is not None:
            out["level"] = level
    return out


def _frames_from_result(value: Any) -> List[Dict[str, Any]]:
    frames: List[Dict[str, Any]] = []
    for item in _ensure_list(value):
        frame = item.get("frame") if isinstance(item, dict) else item
        if isinstance(frame, dict):
            frames.append(normalize_frame(frame))
    return frames


def _named_values(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in _ensure_list(value):
        if isinstance(item, dict):
            if len(item) == 1 and next(iter(item.keys())) in {"arg", "var", "register-values"}:
                inner = next(iter(item.values()))
                if isinstance(inner, dict):
                    out.append(inner)
                    continue
            out.append(item)
    return out


def _unwrap_breakpoint(value: Any) -> Dict[str, Any]:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        if "bkpt" in value and isinstance(value["bkpt"], dict):
            value = value["bkpt"]
        return dict(value)
    return {}


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _maybe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _cap_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _command_name(command: str) -> str:
    return command.strip().split(" ", 1)[0]

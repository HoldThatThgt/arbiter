#!/usr/bin/env python3
from __future__ import annotations

import codecs
import sys
import time


breakpoints = []
target = None
args = []


def main() -> int:
    print('=thread-group-added,id="i1"', flush=True)
    print("(gdb)", flush=True)
    for raw in sys.stdin:
        raw = raw.rstrip("\n")
        token = ""
        while raw and raw[0].isdigit():
            token += raw[0]
            raw = raw[1:]
        command = raw.strip()
        handle(token, command)
        print("(gdb)", flush=True)
    return 0


def handle(token: str, command: str) -> None:
    global target, args
    if command.startswith("-gdb-exit"):
        print(f"{token}^exit", flush=True)
        raise SystemExit(0)
    if command.startswith("-arb-die"):
        # Simulate GDB crashing mid-command: exit (closing stdout to EOF)
        # WITHOUT emitting a result record for this token, so a blocked
        # command() must be woken by the EOF path, not by a reply.
        raise SystemExit(0)
    if command.startswith("-arb-dup"):
        # Simulate GDB emitting two ^result records with the SAME token (a
        # protocol violation). The reader's per-token waiter is a maxsize=1
        # queue, so the surplus record must be dropped rather than block the
        # reader thread on a full queue.
        print(f"{token}^done", flush=True)
        print(f"{token}^done", flush=True)
        return
    if command.startswith("-gdb-set") or command.startswith("-environment-cd"):
        print(f"{token}^done", flush=True)
        return
    if command.startswith("-file-exec-and-symbols"):
        target = unquote(command.split(" ", 1)[1])
        if "bad-debug" in target:
            print(f'{token}^error,msg="DW_FORM_GNU_str_index or DW_FORM_strx used without .debug_str section"', flush=True)
            return
        print(f"{token}^done", flush=True)
        return
    if command.startswith("-exec-arguments"):
        args = command.split(" ")[1:]
        print(f"{token}^done", flush=True)
        return
    if command.startswith("-break-insert"):
        number = str(len(breakpoints) + 1)
        bkpt = {
            "number": number,
            "type": "breakpoint",
            "disp": "keep",
            "enabled": "y",
            "addr": "0x0000000000401130",
            "func": "main",
            "file": "main.c",
            "fullname": target or "/tmp/main.c",
            "line": "3",
            "times": "0",
        }
        breakpoints.append(bkpt)
        print(f"{token}^done,bkpt={fmt_tuple(bkpt)}", flush=True)
        return
    if command.startswith("-break-watch"):
        number = str(len(breakpoints) + 1)
        wpt = {
            "number": number,
            "type": "watchpoint",
            "disp": "keep",
            "enabled": "y",
            "exp": unquote(command.split(" ")[-1]),
            "times": "0",
        }
        breakpoints.append(wpt)
        print(f"{token}^done,wpt={fmt_tuple(wpt)}", flush=True)
        return
    if command.startswith("-break-list"):
        body = ",".join("bkpt=" + fmt_tuple(item) for item in breakpoints)
        print(f'{token}^done,BreakpointTable={{nr_rows="{len(breakpoints)}",body=[{body}]}}', flush=True)
        return
    if command.startswith("-break-delete"):
        breakpoints.clear()
        print(f"{token}^done", flush=True)
        return
    if command.startswith("-break-enable") or command.startswith("-break-disable"):
        print(f"{token}^done", flush=True)
        return
    if command.startswith("-exec-run") or command.startswith("-exec-continue") or command.startswith("-exec-next") or command.startswith("-exec-step") or command.startswith("-exec-finish"):
        print(f"{token}^running", flush=True)
        time.sleep(0.01)
        print('*stopped,reason="breakpoint-hit",thread-id="1",frame={level="0",addr="0x0000000000401130",func="main",file="main.c",fullname="main.c",line="3"}', flush=True)
        return
    if command.startswith("-exec-interrupt"):
        print(f"{token}^done", flush=True)
        print('*stopped,reason="signal-received",signal-name="SIGINT",thread-id="1",frame={level="0",func="main",file="main.c",fullname="main.c",line="3"}', flush=True)
        return
    if command.startswith("-stack-list-frames"):
        print(f'{token}^done,stack=[frame={{level="0",addr="0x401130",func="main",file="main.c",fullname="main.c",line="3"}},frame={{level="1",addr="0x401000",func="_start",file="crt1.c",line="1"}}]', flush=True)
        return
    if command.startswith("-stack-list-arguments"):
        print(f'{token}^done,stack-args=[frame={{level="0",args=[{{name="argc",value="1"}},{{name="argv",value="0x7fffffffe000"}}]}}]', flush=True)
        return
    if command.startswith("-stack-list-locals"):
        print(f'{token}^done,locals=[{{name="x",value="42"}},{{name="ptr",value="0x1000"}}]', flush=True)
        return
    if command.startswith("-thread-info"):
        print(f'{token}^done,threads=[{{id="1",target-id="Thread 1",state="stopped",frame={{level="0",func="main",file="main.c",line="3"}}}}],current-thread-id="1"', flush=True)
        return
    if command.startswith("-thread-select"):
        print(f'{token}^done,new-thread-id="1",frame={{level="0",addr="0x401130",func="main",file="main.c",fullname="main.c",line="3"}}', flush=True)
        return
    if command.startswith("-stack-select-frame"):
        print(f"{token}^done", flush=True)
        return
    if command.startswith("-data-list-register-values"):
        print(f'{token}^done,register-values=[{{number="0",value="0x0"}},{{number="1",value="0x2a"}}]', flush=True)
        return
    if command.startswith("-data-evaluate-expression"):
        print(f'{token}^done,value="42"', flush=True)
        return
    if command.startswith("-data-read-memory-bytes"):
        print(f'{token}^done,memory=[{{begin="0x1000",offset="0x0",end="0x1004",contents="01020304"}}]', flush=True)
        return
    if command.startswith("-interpreter-exec console"):
        text = unquote(command.split(" ", 2)[2])
        if text.startswith("ptype"):
            print('~"type = int\\n"', flush=True)
        else:
            print('~"#0  main () at main.c:3\\n"', flush=True)
        print(f"{token}^done", flush=True)
        return
    if command.startswith("-target-attach") or command.startswith("-target-select"):
        print(f"{token}^done", flush=True)
        print('*stopped,reason="signal-received",signal-name="SIGTRAP",thread-id="1",frame={level="0",func="main",file="main.c",fullname="main.c",line="3"}', flush=True)
        return
    print(f'{token}^error,msg="unsupported command: {escape(command)}"', flush=True)


def unquote(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return codecs.decode(value[1:-1], "unicode_escape")
    return value


def quote(value: str) -> str:
    return '"' + escape(value) + '"'


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def fmt_tuple(payload: dict) -> str:
    return "{" + ",".join(f"{key}={quote(str(value))}" for key, value in payload.items()) + "}"


if __name__ == "__main__":
    raise SystemExit(main())

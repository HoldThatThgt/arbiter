import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from arbiter_engine.gdbmcp.config import Config
from arbiter_engine.gdbmcp.errors import ToolError
from arbiter_engine.gdbmcp.sessions import SessionManager
from arbiter_engine.gdbmcp.tools import _command


FIXTURE = Path(__file__).parent / "fixtures" / "fake_gdb.py"


def fake_gdb_path():
    mode = FIXTURE.stat().st_mode
    FIXTURE.chmod(mode | stat.S_IXUSR)
    return str(FIXTURE)


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "main.c"
        self.source.write_text("int main(void) {\n  int x = 42;\n  return x;\n}\n", encoding="utf-8")
        self.binary = self.root / "demo"
        self.binary.write_text("fake", encoding="utf-8")
        self.config = Config(root=self.root, gdb_path=fake_gdb_path())
        self.manager = SessionManager(self.config)

    def tearDown(self):
        self.manager.close_all()
        self.tmp.cleanup()

    def test_start_run_snapshot_and_stop(self):
        started = self.manager.start(target="demo", cwd=".", run_until="main", wait_ms=500)
        self.assertTrue(started["ok"])
        session = self.manager.get(started["session_id"])
        self.assertEqual(session.state, "stopped")
        bp = session.set_breakpoint("main")
        self.assertEqual(bp["breakpoint"]["func"], "main")
        watch = session.set_breakpoint("x", kind="watch")
        self.assertEqual(watch["breakpoint"]["type"], "watchpoint")
        run = session.run_control("continue", wait_ms=500)
        self.assertEqual(run["state"], "stopped")
        stack = session.stack(include_source=True)
        self.assertEqual(stack["frames"][0]["func"], "main")
        self.assertTrue(stack["source"]["available"])
        snap = session.snapshot()
        self.assertIn("locals", snap)
        self.assertIn("registers", snap)
        stopped = self.manager.stop(started["session_id"])
        self.assertEqual(stopped["stopped"], [started["session_id"]])

    def test_outside_root_is_denied(self):
        outside = Path(self.tmp.name).parent / "outside-demo"
        outside.write_text("fake", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            self.manager.start(target=str(outside), cwd=".")
        self.assertEqual(ctx.exception.code, "path_outside_root")

    def test_bootstrap_failure_is_not_registered_and_has_guidance(self):
        bad = self.root / "bad-debug"
        bad.write_text("fake", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            self.manager.start(target="bad-debug", cwd=".")
        self.assertEqual(ctx.exception.code, "gdb_error")
        self.assertEqual(ctx.exception.details["guidance"]["kind"], "debug_info_format_unsupported")
        self.assertEqual(self.manager.list(), [])

    def test_source_context_does_not_leak_outside_root_path(self):
        started = self.manager.start(target="demo", cwd=".")
        session = self.manager.get(started["session_id"])
        outside = Path(self.tmp.name).parent / "secret" / "outside.c"
        result = session.source_context({"fullname": str(outside), "line": "1"})
        self.assertEqual(result["path"], "outside.c")
        self.assertEqual(result["error"], "path_outside_root")

    def test_remote_mode_is_explicit_opt_in(self):
        with self.assertRaises(ToolError) as ctx:
            self.manager.start(mode="remote", remote_endpoint="localhost:1234")
        self.assertEqual(ctx.exception.code, "remote_disabled")

    def test_remote_mode_connects_when_enabled(self):
        manager = SessionManager(Config(root=self.root, gdb_path=fake_gdb_path(), allow_remote=True))
        try:
            started = manager.start(mode="remote", remote_endpoint="localhost:1234", wait_ms=500)
            self.assertEqual(started["mode"], "remote")
            self.assertEqual(started["state"], "stopped")
        finally:
            manager.close_all()

    def test_gdb_death_mid_command_wakes_immediately_as_session_exited(self):
        # GDB dying while a command is in flight must wake the blocked call
        # immediately with session_exited, NOT hang for the full timeout and
        # then misreport gdb_timeout.
        started = self.manager.start(target="demo", cwd=".")
        session = self.manager.get(started["session_id"])
        timeout_ms = 8000
        start = time.monotonic()
        with self.assertRaises(ToolError) as ctx:
            session.command("-arb-die", timeout_ms=timeout_ms)
        elapsed = time.monotonic() - start
        self.assertEqual(ctx.exception.code, "session_exited")
        # Must wake on EOF, far below the timeout ceiling (allow generous slack
        # for slow CI, but nowhere near the full 8s timeout).
        self.assertLess(elapsed, 2.0)

    def test_duplicate_token_result_does_not_wedge_reader(self):
        # GDB emitting two ^result records with the SAME token (a protocol
        # violation the MI parser does not dedupe) must not wedge the reader
        # thread on a full maxsize=1 waiter queue. The first result is delivered
        # normally; the surplus is dropped (non-blocking put), and the reader
        # keeps draining stdout so a subsequent command still completes.
        started = self.manager.start(target="demo", cwd=".")
        session = self.manager.get(started["session_id"])
        result = session.command("-arb-dup", timeout_ms=2000)
        self.assertEqual(result.record.cls, "done")
        # A follow-up command must complete (it would hang to timeout if the
        # duplicate had blocked the reader thread), and the reader stays alive.
        again = session.command("-arb-dup", timeout_ms=2000)
        self.assertEqual(again.record.cls, "done")
        self.assertTrue(session._reader.is_alive(), "reader thread wedged by duplicate-token result")

    def test_multistatement_console_command_is_rejected(self):
        # A dangerous keyword hidden past the first token must not slip through
        # the deny-by-default console guard, whatever separator hides it:
        # newline, semicolon (with or without surrounding space), pipe, or a
        # Unicode line/paragraph separator.
        started = self.manager.start(target="demo", cwd=".")
        session_id = started["session_id"]
        for command in (
            "print 1\nshell id",
            "print 1; shell id",
            "print 1;shell id",
            "print 1|shell id",
            "print 1 shell id",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ToolError) as ctx:
                    _command(self.manager, self.config, {"session_id": session_id, "command": command})
                self.assertEqual(ctx.exception.code, "dangerous_command_denied")


if __name__ == "__main__":
    unittest.main()

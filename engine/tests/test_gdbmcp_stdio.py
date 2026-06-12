import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "fake_gdb.py"


def fake_gdb_path():
    mode = FIXTURE.stat().st_mode
    FIXTURE.chmod(mode | stat.S_IXUSR)
    return str(FIXTURE)


class StdioServerTest(unittest.TestCase):
    def test_stdio_process_handles_initialize_list_and_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo").write_text("fake", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "arbiter_engine.gdbmcp",
                    "serve",
                    "--root",
                    str(root),
                    "--gdb",
                    fake_gdb_path(),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
            )
            try:
                init = call(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
                self.assertEqual(init["result"]["serverInfo"]["name"], "gdb-mcp")
                tools = call(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                self.assertIn("gdb_start", {tool["name"] for tool in tools["result"]["tools"]})
                started = call(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "gdb_start", "arguments": {"target": "demo", "run_until": "main", "wait_ms": 500}},
                    },
                )
                self.assertFalse(started["result"]["isError"])
                self.assertEqual(started["result"]["structuredContent"]["state"], "stopped")
            finally:
                try:
                    call(proc, {"jsonrpc": "2.0", "id": 99, "method": "shutdown"})
                except Exception:
                    pass
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass


def call(proc, payload):
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        raise AssertionError(f"server closed stdout; stderr={stderr}")
    return json.loads(line)


if __name__ == "__main__":
    unittest.main()

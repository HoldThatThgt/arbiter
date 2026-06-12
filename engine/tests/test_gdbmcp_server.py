import json
import stat
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.gdbmcp.config import Config
from arbiter_engine.gdbmcp.server import MCPServer


FIXTURE = Path(__file__).parent / "fixtures" / "fake_gdb.py"


def fake_gdb_path():
    mode = FIXTURE.stat().st_mode
    FIXTURE.chmod(mode | stat.S_IXUSR)
    return str(FIXTURE)


def request(method, params=None, request_id=1):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "demo").write_text("fake", encoding="utf-8")
        (self.root / "main.c").write_text("int main(void) {\n  return 0;\n}\n", encoding="utf-8")
        self.server = MCPServer(Config(root=self.root, gdb_path=fake_gdb_path()))

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def test_initialize_and_tools_list(self):
        response = self.server.handle(request("initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual(response["result"]["serverInfo"]["name"], "gdb-mcp")
        tools = self.server.handle(request("tools/list"))["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertIn("gdb_start", names)
        self.assertIn("gdb_snapshot", names)

    def test_diagnostics_tool_returns_checks(self):
        response = self.server.handle(request("tools/call", {"name": "gdb_diagnostics", "arguments": {}}))
        self.assertIn("checks", response["result"]["structuredContent"])
        self.assertIn("gdb", {check["name"] for check in response["result"]["structuredContent"]["checks"]})

    def test_call_tool_structured_content(self):
        response = self.server.handle(
            request(
                "tools/call",
                {"name": "gdb_start", "arguments": {"target": "demo", "run_until": "main", "wait_ms": 500}},
            )
        )
        self.assertFalse(response["result"]["isError"])
        structured = response["result"]["structuredContent"]
        self.assertEqual(structured["state"], "stopped")
        session_id = structured["session_id"]
        snap = self.server.handle(
            request("tools/call", {"name": "gdb_snapshot", "arguments": {"session_id": session_id}}, request_id=2)
        )
        self.assertIn("locals", snap["result"]["structuredContent"])
        selected = self.server.handle(
            request(
                "tools/call",
                {"name": "gdb_select", "arguments": {"session_id": session_id, "thread_id": "1", "frame_level": 0}},
                request_id=3,
            )
        )
        self.assertFalse(selected["result"]["isError"])
        self.assertEqual(selected["result"]["structuredContent"]["current_frame"]["func"], "main")

    def test_invalid_argument_rejected_as_protocol_error(self):
        response = self.server.handle(
            request("tools/call", {"name": "gdb_sessions", "arguments": {"unknown": True}})
        )
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32602)

    def test_dangerous_command_is_tool_error(self):
        start = self.server.handle(
            request("tools/call", {"name": "gdb_start", "arguments": {"target": "demo"}}, request_id=1)
        )
        session_id = start["result"]["structuredContent"]["session_id"]
        response = self.server.handle(
            request("tools/call", {"name": "gdb_command", "arguments": {"session_id": session_id, "command": "shell id"}}, request_id=2)
        )
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["error"]["code"], "dangerous_command_denied")


if __name__ == "__main__":
    unittest.main()

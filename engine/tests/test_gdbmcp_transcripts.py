import json
import re
import stat
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.gdbmcp.config import Config
from arbiter_engine.gdbmcp.server import MCPServer


ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixtures" / "fake_gdb.py"
TRANSCRIPT = ROOT / "fixtures" / "gdb_basic_session.jsonl"


class TranscriptTest(unittest.TestCase):
    def test_basic_session_transcript_replays_exactly(self):
        messages = _load_transcript(TRANSCRIPT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo").write_text("fake", encoding="utf-8")
            (root / "main.c").write_text("int main(void) {\n  return 0;\n}\n", encoding="utf-8")
            FIXTURE.chmod(FIXTURE.stat().st_mode | stat.S_IXUSR)
            server = MCPServer(Config(root=root, gdb_path=str(FIXTURE)))
            session_id = None
            try:
                for index in range(0, len(messages), 2):
                    request = messages[index]
                    expected = messages[index + 1]
                    self.assertEqual(request["type"], "request")
                    self.assertEqual(expected["type"], "response")
                    actual_message = server.handle(_materialize_request(request["message"], session_id))
                    actual = {"type": "response", "message": actual_message}
                    maybe_session = _extract_session_id(actual_message)
                    if maybe_session is not None:
                        session_id = maybe_session
                    self.assertEqual(_normalize(actual), expected)
            finally:
                server.close()

    def test_tool_descriptor_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            FIXTURE.chmod(FIXTURE.stat().st_mode | stat.S_IXUSR)
            server = MCPServer(Config(root=Path(tmp), gdb_path=str(FIXTURE)))
            try:
                response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            finally:
                server.close()
        tools = response["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertEqual(
            names,
            [
                "gdb_breakpoint",
                "gdb_command",
                "gdb_diagnostics",
                "gdb_eval",
                "gdb_exec",
                "gdb_memory",
                "gdb_select",
                "gdb_sessions",
                "gdb_snapshot",
                "gdb_stack",
                "gdb_start",
                "gdb_stop",
            ],
        )
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIs(tool["inputSchema"]["additionalProperties"], False)
            self.assertEqual(tool["outputSchema"]["required"], ["ok"])


def _load_transcript(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _materialize_request(value, session_id):
    if isinstance(value, dict):
        return {key: _materialize_request(item, session_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_materialize_request(item, session_id) for item in value]
    if value == "<session_id>":
        if session_id is None:
            raise AssertionError("transcript requested a session before one was created")
        return session_id
    return value


def _extract_session_id(message):
    try:
        session_id = message["result"]["structuredContent"]["session_id"]
    except (KeyError, TypeError):
        return None
    return session_id if isinstance(session_id, str) else None


def _normalize(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in {"session_id", "created_at", "ts"}:
                out[key] = "<volatile>"
            else:
                out[key] = _normalize(item)
        return out
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"session=[0-9a-f]{12}", "session=<volatile>", value)
    return value


if __name__ == "__main__":
    unittest.main()

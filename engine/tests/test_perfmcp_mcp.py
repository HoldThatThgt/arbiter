from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arbiter_engine.perfmcp.mcp import MCPServer


class MCPProtocolTests(unittest.TestCase):
    def test_initialize_list_tools_and_call_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hot.c").write_text(
                "int f(char *s) { int n = 0; for (int i = 0; i < strlen(s); i++) n += i; return n; }\n",
                encoding="utf-8",
            )
            server = MCPServer()

            init = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}},
                }
            )
            self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")  # type: ignore[index]

            tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            names = [tool["name"] for tool in tools["result"]["tools"]]  # type: ignore[index]
            self.assertEqual(
                names,
                ["perf.scan_c", "perf.explain_finding", "perf.measure_command", "perf.toolchain_probe"],
            )

            scan = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "perf.scan_c",
                        "arguments": {"root": str(root), "min_severity": "low"},
                    },
                }
            )
            result = scan["result"]  # type: ignore[index]
            self.assertFalse(result["isError"])
            structured = result["structuredContent"]
            self.assertEqual(structured["schema_version"], "perf-mcp.scan.v1")
            self.assertGreaterEqual(structured["summary"]["finding_count"], 1)

            finding = structured["findings"][0]
            explanation = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "perf.explain_finding",
                        "arguments": {
                            "analysis_id": structured["analysis_id"],
                            "finding_id": finding["id"],
                        },
                    },
                }
            )
            self.assertFalse(explanation["result"]["isError"])  # type: ignore[index]

    def test_unknown_tool_is_json_rpc_error(self) -> None:
        server = MCPServer()
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "missing", "arguments": {}},
            }
        )
        self.assertEqual(response["error"]["code"], -32602)  # type: ignore[index]

    def _call(self, name: str, arguments: dict) -> dict:
        return MCPServer().handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    def test_unknown_argument_key_is_rejected(self) -> None:
        response = self._call("perf.toolchain_probe", {"totally_unknown_key": 123})
        self.assertNotIn("result", response)
        error = response["error"]  # type: ignore[index]
        self.assertEqual(error["code"], -32602)
        self.assertEqual(error["data"]["kind"], "invalid_arguments")
        self.assertEqual(error["data"]["reason"], "unknown_arguments")
        self.assertEqual(error["data"]["fields"], ["totally_unknown_key"])

    def test_out_of_range_value_is_rejected_not_clamped(self) -> None:
        response = self._call(
            "perf.measure_command",
            {"command": ["true"], "repeat": 99999, "timeout_seconds": 999999},
        )
        self.assertNotIn("result", response)
        error = response["error"]  # type: ignore[index]
        self.assertEqual(error["code"], -32602)
        self.assertEqual(error["data"]["kind"], "invalid_arguments")
        self.assertEqual(error["data"]["reason"], "too_large")
        self.assertEqual(error["data"]["field"], "repeat")

    def test_missing_required_argument_is_rejected(self) -> None:
        response = self._call("perf.measure_command", {"repeat": 1})
        self.assertNotIn("result", response)
        error = response["error"]  # type: ignore[index]
        self.assertEqual(error["code"], -32602)
        self.assertEqual(error["data"]["reason"], "missing_required")
        self.assertEqual(error["data"]["field"], "command")

    def test_valid_in_range_arguments_still_dispatch(self) -> None:
        response = self._call("perf.toolchain_probe", {})
        self.assertFalse(response["result"]["isError"])  # type: ignore[index]

    def test_null_optional_argument_uses_default_not_rejected(self) -> None:
        # Explicit null on an optional field is treated as "use default", matching the
        # handlers that read arguments.get(key) and fall back when it is None.
        response = self._call("perf.toolchain_probe", {"root": None})
        self.assertFalse(response["result"]["isError"])  # type: ignore[index]

    def test_null_required_argument_is_rejected(self) -> None:
        response = self._call("perf.measure_command", {"command": None})
        self.assertNotIn("result", response)
        self.assertEqual(response["error"]["data"]["reason"], "bad_type")  # type: ignore[index]
        self.assertEqual(response["error"]["data"]["field"], "command")  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()

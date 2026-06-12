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


if __name__ == "__main__":
    unittest.main()

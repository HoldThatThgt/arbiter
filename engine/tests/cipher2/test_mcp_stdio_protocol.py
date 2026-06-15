# New-native rewrite of cipher-2 tests/test_mcp_stdio_protocol.py (M4 acceptance).
# cipher-2's MCP envelope (serve_stdio, protocolVersion "2025-06-18", serverInfo, ping, error
# data.code strings) does not transfer — arbiter's JSON-RPC chassis is a different protocol. These
# tests are rewritten against arbiter's rpc.serve: initialize returns {engine, version, capabilities},
# tools are listed sorted (detail, search), there is no ping, and protocol errors carry data.kind
# (invalid_json / invalid_request for a batch / method_not_found / tool_not_found). The real nugget
# kept verbatim in spirit: a tools/call search over a populated store returns result_count via the
# real serve loop, and protocol errors are JSON-RPC errors (no "result"), never tool results.
import io
import json
import tempfile
import unittest
from pathlib import Path

from arbiter_engine import rpc
from arbiter_engine.facts.store import FactRecord, open_fact_store

from ._facts_server import engine_env, working_dir


def _line(payload):
    return json.dumps(payload, sort_keys=True) + "\n"


class ArbiterStdioProtocolTest(unittest.TestCase):
    def test_stdio_initialize_list_and_search_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(
                [
                    FactRecord(
                        object_id="fact:one",
                        object_name="One",
                        object_description="alpha",
                        object_source="src/one.c:1",
                        object_profile="debug",
                    )
                ]
            )
            input_stream = io.StringIO(
                _line({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                + _line({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                + _line(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "search", "arguments": {"query": "alpha", "limit": 1}},
                    }
                )
            )
            output_stream = io.StringIO()
            with working_dir(target), engine_env("QUERY", "player"):
                rpc.serve(input_stream, output_stream)

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertEqual(responses[0]["result"]["engine"], "arbiter-engine")
        self.assertIn("version", responses[0]["result"])
        self.assertTrue(responses[0]["result"]["capabilities"]["tools"])
        # The raw engine rpc exposes all engine tools (search/detail/run/recipe_search/register/
        # import_recipes/scan); per-seat RBAC filtering happens in the Go seat layer, not here.
        tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertIn("search", tool_names)
        self.assertIn("detail", tool_names)
        self.assertEqual(responses[2]["result"]["structuredContent"]["result_count"], 1)
        self.assertNotIn("debug", output_stream.getvalue().splitlines()[0].lower())

    def test_stdio_protocol_errors_are_json_rpc_errors_not_tool_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            input_stream = io.StringIO(
                "{bad json}\n"
                + _line([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
                + _line({"jsonrpc": "2.0", "id": 2, "method": "unknown", "params": {}})
                + _line({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "missing", "arguments": {}}})
            )
            output_stream = io.StringIO()
            with working_dir(target), engine_env("QUERY", "player"):
                rpc.serve(input_stream, output_stream)

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(
            [response["error"]["data"]["kind"] for response in responses],
            ["invalid_json", "invalid_request", "method_not_found", "tool_not_found"],
        )
        self.assertTrue(all("result" not in response for response in responses))


if __name__ == "__main__":
    unittest.main()

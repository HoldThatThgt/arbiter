import io
import json
import tempfile
import unittest
from pathlib import Path

from cipher2.mcp import serve_stdio
from cipher2.storage import FactRecord, open_fact_store


def _line(payload):
    return json.dumps(payload, sort_keys=True) + "\n"


class McpStdioProtocolTest(unittest.TestCase):
    def test_stdio_initialize_list_ping_and_search_call(self):
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
                + _line({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
                + _line({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                + _line({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}})
                + _line(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "search", "arguments": {"query": "alpha", "limit": 1}},
                    }
                )
            )
            output_stream = io.StringIO()

            exit_code = serve_stdio(target, input=input_stream, output=output_stream)

        self.assertEqual(exit_code, 0)
        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "1.0.0")
        self.assertEqual([tool["name"] for tool in responses[1]["result"]["tools"]], ["search", "detail"])
        self.assertEqual(responses[2]["result"], {})
        self.assertEqual(responses[3]["result"]["structuredContent"]["result_count"], 1)
        self.assertNotIn("debug", output_stream.getvalue().splitlines()[0].lower())

    def test_stdio_protocol_errors_are_json_rpc_errors_not_tool_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            input_stream = io.StringIO(
                "{bad json}\n"
                + _line([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
                + _line({"jsonrpc": "2.0", "id": 2, "method": "unknown", "params": {}})
                + _line({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "missing", "arguments": {}}})
            )
            output_stream = io.StringIO()

            serve_stdio(target, input=input_stream, output=output_stream)

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual([response["error"]["data"]["code"] for response in responses], ["malformed_json", "unsupported_batch", "unknown_method", "unknown_tool"])
        self.assertTrue(all("result" not in response for response in responses))


if __name__ == "__main__":
    unittest.main()

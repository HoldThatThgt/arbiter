import tempfile
import unittest
from pathlib import Path

from cipher2.mcp import McpError, ToolCallResult, ToolDescriptor, open_mcp_server


class McpToolModelsTest(unittest.TestCase):
    def test_open_server_validates_target_repo_and_log_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = open_mcp_server(Path(tmp), log_enabled=True)
            self.assertEqual(server.target_repo, Path(tmp))
            self.assertTrue(server.log_enabled)

            with self.assertRaises(McpError) as bad_repo:
                open_mcp_server(Path(tmp) / "missing")
            self.assertEqual(bad_repo.exception.code, "invalid_target_repo")

            with self.assertRaises(McpError) as bad_log_enabled:
                open_mcp_server(Path(tmp), log_enabled="yes")
            self.assertEqual(bad_log_enabled.exception.code, "invalid_log_enabled")

            with self.assertRaises(McpError) as bad_provider:
                open_mcp_server(Path(tmp), fact_view_provider="view")
            self.assertEqual(bad_provider.exception.code, "invalid_fact_view_provider")

    def test_list_tools_declares_only_search_detail_input_and_output_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = open_mcp_server(Path(tmp)).list_tools().tools

        self.assertEqual([tool.name for tool in tools], ["search", "detail"])
        for tool in tools:
            self.assertIsInstance(tool, ToolDescriptor)
            self.assertEqual(tool.input_schema["type"], "object")
            self.assertEqual(tool.output_schema["type"], "object")
            self.assertIn("properties", tool.input_schema)
            self.assertIn("properties", tool.output_schema)

        search = tools[0]
        self.assertEqual(search.input_schema["properties"]["limit"]["default"], 20)
        self.assertEqual(search.input_schema["properties"]["limit"]["minimum"], 1)
        self.assertEqual(search.input_schema["properties"]["limit"]["maximum"], 50)
        self.assertIn("readers:<field>", search.description)
        self.assertIn("writers:<field>", search.description)
        self.assertIn("accessors:<field>", search.description)
        self.assertIn("callers:<func>", search.description)
        self.assertIn("callees:<func>", search.description)
        self.assertIn("depth:<N>", search.description)
        self.assertIn("reachable:<from>-><to>", search.description)
        self.assertIn("path nodes may include condition", search.description)
        self.assertIn("logical AND of hop conditions", search.description)
        self.assertIn("path[2].condition", search.description)
        self.assertIn("reset_flag", search.description)
        self.assertIn("file:<path>", search.description)
        self.assertIn("name:<func>", search.description)
        self.assertIn("search('value NullableDatum')", search.description)
        self.assertIn("search('writers:<field_object_id>')", search.description)
        self.assertIn("accessors:<field_object_id>", search.description)
        self.assertNotIn("NullableDatum.value", search.description)
        self.assertNotIn("Type.field", search.description)
        self.assertIn("detail('<field_object_id>')", search.description)
        self.assertIn("too_broad", search.description)
        self.assertIn("anchor ambiguity", search.description)
        self.assertIn("limit is 1..50", search.description)
        self.assertNotIn("scope", search.input_schema["properties"])
        self.assertIn("results", search.output_schema["properties"])
        self.assertIn("view_state", search.output_schema["required"])
        self.assertIn("overlay_id", search.output_schema["properties"])
        self.assertEqual(
            search.output_schema["properties"]["query_kind"]["enum"],
            ["empty", "terms", "relation", "relation_transitive", "relation_reachable"],
        )
        self.assertIn("complete", search.output_schema["properties"])
        self.assertIn("budget_exhausted", search.output_schema["properties"])
        self.assertIn("path", search.output_schema["properties"])

        detail = tools[1]
        self.assertEqual(detail.input_schema["properties"]["budget"]["default"], "normal")
        self.assertEqual(detail.input_schema["properties"]["budget"]["enum"], ["small", "normal", "large"])
        self.assertIn("fact_id from a search result object_id", detail.description)
        self.assertIn("budget small/normal/large", detail.description)
        self.assertIn("source_context", detail.description)
        self.assertIn("relative_preview", detail.description)
        self.assertIn("relative_preview navigation", detail.description)
        self.assertIn("callers/callees/field buckets", detail.description)
        self.assertIn("total_count", detail.description)
        self.assertIn("shown_count", detail.description)
        self.assertIn("truncated", detail.description)
        self.assertNotIn("scope", detail.input_schema["properties"])
        self.assertNotIn("ref", detail.input_schema["properties"])
        self.assertIn("source_context", detail.output_schema["properties"])
        self.assertIn("relative_preview", detail.output_schema["properties"])
        self.assertIn("base_snapshot_id", detail.output_schema["required"])

    def test_call_tool_rejects_unknown_tool_as_structured_mcp_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = open_mcp_server(Path(tmp)).call_tool("unknown", {})

        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "unknown_tool")
        self.assertIn("unknown_tool", result.content[0]["text"])

    def test_impact_is_not_exposed_as_mcp_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = open_mcp_server(Path(tmp))

            result = server.call_tool("impact", {"ref": {"ref_kind": "fact", "ref_id": "fact:a"}})

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "unknown_tool")

    def test_relations_is_not_exposed_as_mcp_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = open_mcp_server(Path(tmp))

            result = server.call_tool("relations", {"fact_id": "fact:a"})

        self.assertFalse(hasattr(server, "relations"))
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "unknown_tool")

    def test_search_and_detail_reject_removed_scope_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = open_mcp_server(Path(tmp))

            search = server.call_tool("search", {"query": "alpha", "scope": "graph"})
            detail = server.call_tool("detail", {"fact_id": "fact:a", "scope": "fact"})

        self.assertTrue(search.is_error)
        self.assertEqual(search.structured_content["error"]["code"], "invalid_args")
        self.assertTrue(detail.is_error)
        self.assertEqual(detail.structured_content["error"]["code"], "invalid_args")


if __name__ == "__main__":
    unittest.main()

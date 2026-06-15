# Migrated from cipher-2 tests/test_mcp_tool_models.py (M4 acceptance).
# Adaptations: arbiter has no McpServer constructor (the rpc loop binds to cwd+Context), so the
# list-tools assertions read arbiter's frozen ToolDescriptor objects directly, and the tool-domain
# rejections that cipher-2 surfaced as in-band error results are arbiter PROTOCOL errors (RPCError):
# unknown tool -> -32601/tool_not_found, removed `scope` arg -> -32602/invalid_args (additionalProperties:false).
# The open_mcp_server-constructor-validation test is dropped (no arbiter analog).
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.errors import RPCError
from arbiter_engine.facts.descriptors import ToolDescriptor, _detail_descriptor, _search_descriptor

from ._facts_server import open_facts_server


class McpToolModelsTest(unittest.TestCase):
    def test_list_tools_declares_only_search_detail_input_and_output_schema(self):
        tools = [_search_descriptor(), _detail_descriptor()]

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
            with self.assertRaises(RPCError) as cm:
                open_facts_server(Path(tmp)).call_tool("unknown", {})
        self.assertEqual(cm.exception.code, -32601)
        self.assertEqual(cm.exception.data["kind"], "tool_not_found")

    def test_impact_is_not_exposed_as_mcp_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = open_facts_server(Path(tmp))
            with self.assertRaises(RPCError) as cm:
                server.call_tool("impact", {"ref": {"ref_kind": "fact", "ref_id": "fact:a"}})
        self.assertEqual(cm.exception.code, -32601)
        self.assertEqual(cm.exception.data["kind"], "tool_not_found")

    def test_relations_is_not_exposed_as_mcp_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = open_facts_server(Path(tmp))
            self.assertFalse(hasattr(server, "relations"))
            with self.assertRaises(RPCError) as cm:
                server.call_tool("relations", {"fact_id": "fact:a"})
        self.assertEqual(cm.exception.code, -32601)
        self.assertEqual(cm.exception.data["kind"], "tool_not_found")

    def test_search_and_detail_reject_removed_scope_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = open_facts_server(Path(tmp))
            with self.assertRaises(RPCError) as search_cm:
                server.call_tool("search", {"query": "alpha", "scope": "graph"})
            with self.assertRaises(RPCError) as detail_cm:
                server.call_tool("detail", {"fact_id": "fact:a", "scope": "fact"})
        self.assertEqual(search_cm.exception.code, -32602)
        self.assertEqual(search_cm.exception.data["kind"], "invalid_args")
        self.assertEqual(detail_cm.exception.code, -32602)
        self.assertEqual(detail_cm.exception.data["kind"], "invalid_args")


if __name__ == "__main__":
    unittest.main()

# Migrated from cipher-2 tests/test_mcp_search_detail.py (M4 acceptance).
# Adaptations: open_mcp_server -> open_facts_server (rpc shim); cipher2.storage ->
# arbiter_engine.facts.store; the two assertIsInstance(SearchResponse/DetailResponse) checks
# are dropped (the shim returns attribute-view wrappers over structuredContent, not the dataclasses).
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.facts.store import FactRecord, open_fact_store

from ._facts_server import open_facts_server


def _fact(object_id: str, name: str, source: str, **overrides):
    data = {
        "object_id": object_id,
        "object_name": name,
        "object_description": f"{name} alpha helper",
        "object_source": source,
        "object_profile": "debug",
        "object_caller": None,
        "object_callee": None,
        "payload": {"fact_kind": "function", "rank": 1, "notes": "short"},
    }
    data.update(overrides)
    return FactRecord(**data)


class McpSearchDetailTest(unittest.TestCase):
    def test_search_returns_fact_summaries_with_empty_query_object_id_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(
                [
                    _fact("fact:b", "Beta", "src/beta.c:2"),
                    _fact("fact:a", "Alpha", "src/alpha.c:1"),
                    _fact("fact:c", "Gamma", "src/gamma.c:3"),
                ]
            )

            response = open_facts_server(target).search("", limit=2)

        self.assertEqual(response.query, "")
        self.assertEqual(response.limit, 2)
        self.assertEqual(response.result_count, 2)
        self.assertEqual([item.object_id for item in response.results], ["fact:a", "fact:b"])
        self.assertTrue(response.truncated)

    def test_search_tool_call_returns_structured_content_and_text_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(
                [
                    _fact("fact:a", "Alpha", "src/alpha.c:1", object_description="entry point"),
                    _fact("fact:b", "Beta", "src/beta.c:2", object_description="caller only"),
                ]
            )

            result = open_facts_server(target).call_tool("search", {"query": "alpha", "limit": 1})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["result_count"], 1)
        self.assertEqual(result.structured_content["results"][0]["object_id"], "fact:a")
        self.assertIn("fact:a", result.content[0]["text"])
        self.assertNotIn("storage adapter", result.content[0]["text"].casefold())

    def test_search_exact_identifier_without_exact_object_name_reports_fallback_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(
                [
                    _fact(
                        "fact:function:init_var_from_num",
                        "init_var_from_num",
                        "src/backend/utils/adt/numeric.c:7211",
                    )
                ]
            )

            result = open_facts_server(target).call_tool("search", {"query": "init_var", "limit": 5})

        self.assertFalse(result.is_error)
        content = result.structured_content
        self.assertEqual(content["status"], "ok")
        self.assertEqual(content["result_count"], 1)
        self.assertEqual(content["results"][0]["object_name"], "init_var_from_num")
        self.assertIn("No exact object_name match for `init_var`", content["message"])
        self.assertIn("init_var_from_num", content["message"])
        self.assertIn("No exact object_name match for `init_var`", result.content[0]["text"])

    def test_search_multi_term_uses_and_semantics_order_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(
                [
                    _fact("fact:free", "release_buffer", "src/memory.c:1", object_description="free temporary allocation"),
                    _fact("fact:member", "touch_member", "src/state.c:2", object_description="member update"),
                    _fact("fact:both", "free_member", "src/state.c:3", object_description="free member field"),
                ]
            )
            server = open_facts_server(target)

            first = server.search("free member", limit=10)
            second = server.search("member free", limit=10)

        self.assertEqual([item.object_id for item in first.results], ["fact:both"])
        self.assertEqual([item.object_id for item in second.results], ["fact:both"])

    def test_search_returns_owner_qualified_field_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(
                [
                    _fact(
                        f"global:type:{index:02d}",
                        "type",
                        f"src/global{index}.c:1",
                        object_description="common type global",
                        payload={"fact_kind": "global"},
                    )
                    for index in range(25)
                ]
                + [
                    _fact(
                        "field:Node:type",
                        "type",
                        "src/nodes.h:7",
                        object_description="node discriminator slot",
                        payload={"fact_kind": "field", "owner_name": "Node"},
                    ),
                    _fact(
                        "field:TypeCast:type",
                        "type",
                        "src/parse.h:11",
                        object_description="type cast slot",
                        payload={"fact_kind": "field", "owner_name": "TypeCast"},
                    ),
                ]
            )

            response = open_facts_server(target).search("Node.type", limit=5)

        self.assertEqual(response.results[0].object_id, "field:Node:type")
        self.assertEqual(response.results[0].payload_preview["owner_name"], "Node")

    def test_detail_returns_payload_and_source_context_for_object_source_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "alpha.c"
            source.parent.mkdir()
            source.write_text("\n".join([f"line {line}" for line in range(1, 40)]) + "\n", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(
                [
                    _fact(
                        "fact:alpha",
                        "Alpha",
                        "src/alpha.c:20",
                        object_caller="entry",
                        object_callee="worker",
                        payload={"fact_kind": "function", "signature": "int alpha(void)", "rank": 7},
                    )
                ]
            )

            response = open_facts_server(target).detail("fact:alpha", budget="normal")

        self.assertEqual(response.fact.object_id, "fact:alpha")
        self.assertEqual(response.payload["signature"], "int alpha(void)")
        self.assertFalse(response.payload_truncated)
        self.assertIsNotNone(response.source_context)
        self.assertEqual(response.source_context.source, "src/alpha.c")
        self.assertLessEqual(response.source_context.start_line, 20)
        self.assertGreaterEqual(response.source_context.end_line, 20)
        self.assertIn("line 20", "\n".join(response.source_context.lines))

    def test_detail_tool_call_returns_not_found_error_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = open_facts_server(Path(tmp)).call_tool("detail", {"fact_id": "missing", "budget": "normal"})

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "not_found")
        message = result.structured_content["error"]["message"]
        self.assertIn("missing", result.content[0]["text"])
        self.assertIn("current snapshot", message)
        self.assertIn("search('<symbol name>')", message)
        self.assertIn("valid object_id", message)


if __name__ == "__main__":
    unittest.main()

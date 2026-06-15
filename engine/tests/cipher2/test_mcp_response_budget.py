# Migrated from cipher-2 tests/test_mcp_response_budget.py (M4 acceptance) — PARTIAL.
# Adaptations: open_mcp_server -> open_facts_server; cipher2 -> arbiter_engine.facts.{query,store}.
# Dropped assertions that read NON-serialized ladder bookkeeping (DetailResponse.response_bytes/
# response_bytes_limit/response_truncated, RelationPreview.bucket_relative_dropped_count/
# bucket_dropped_count) — those live only on the in-engine dataclass, never in structuredContent
# (serializing response_bytes would make the byte count self-referential), so they are unreachable
# through the rpc surface. Also dropped the cipher2.tools.log mcp.detail event asserts (store runs
# log-disabled). Every SERIALIZED budget-ladder behavior is kept: payload truncation by budget,
# source-context line shrinking small<normal<large, the real response-byte ceiling, relative_preview
# budget_exhausted/kind + total_count + per-bucket truncation, and the bounded search payload_preview.
import json
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.facts.query import BUDGETS
from arbiter_engine.facts.store import FactRecord, FactRelative, open_fact_store

from ._facts_server import open_facts_server


class McpResponseBudgetTest(unittest.TestCase):
    def test_detail_budget_controls_payload_and_source_context_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "budget.c"
            source.parent.mkdir()
            source.write_text("\n".join([f"line {line}" for line in range(1, 120)]) + "\n", encoding="utf-8")
            payload = {"fact_kind": "function", **{f"field_{index}": "x" * 10 for index in range(80)}}
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(
                [
                    FactRecord(
                        object_id="fact:budget",
                        object_name="Budget",
                        object_description="large payload",
                        object_source="src/budget.c:60",
                        object_profile="debug",
                        payload=payload,
                    )
                ]
            )
            server = open_facts_server(target)

            small = server.detail("fact:budget", budget="small")
            normal = server.detail("fact:budget", budget="normal")
            large = server.detail("fact:budget", budget="large")

        self.assertTrue(small.payload_truncated)
        self.assertLessEqual(len(small.payload), 16)
        self.assertLess(len(small.source_context.lines), len(normal.source_context.lines))
        self.assertLess(len(normal.source_context.lines), len(large.source_context.lines))
        self.assertLessEqual(len(large.payload), 64)
        self.assertTrue(large.payload_truncated)
        # dropped: normal.response_truncated / response_bytes / response_bytes_limit (non-serialized ladder state)

    def test_detail_large_response_respects_declared_response_bytes(self):
        relation_kinds = [
            "direct_call",
            "field_read",
            "field_write",
            "has_field",
            "assigned_to",
            "dispatches_via",
            "include",
            "defines",
            "declares",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "target.c"
            source.parent.mkdir()
            source.write_text("\n".join(f"line {line}" for line in range(1, 80)) + "\n", encoding="utf-8")
            facts = [
                FactRecord(
                    object_id="fact:target",
                    object_name="target",
                    object_description="target function",
                    object_source="src/target.c:40",
                    object_profile="debug",
                    payload={"fact_kind": "function", **{f"payload_{index:02d}": "value" * 5 for index in range(80)}},
                )
            ]
            relatives = []
            for relation_kind in relation_kinds:
                for direction in ("incoming", "outgoing"):
                    for index in range(60):
                        endpoint_id = f"fact:{direction}:{relation_kind}:{index:02d}"
                        facts.append(
                            FactRecord(
                                object_id=endpoint_id,
                                object_name=f"{direction}_{relation_kind}_{index:02d}",
                                object_description="endpoint " + ("x" * 80),
                                object_source=f"src/{relation_kind}_{direction}.c:{index + 1}",
                                object_profile="debug",
                                payload={"fact_kind": "function", "note": "endpoint" * 10},
                            )
                        )
                        from_id = endpoint_id if direction == "incoming" else "fact:target"
                        to_id = "fact:target" if direction == "incoming" else endpoint_id
                        relatives.append(
                            FactRelative(
                                relative_id=f"rel:{direction}:{relation_kind}:{index:02d}",
                                from_fact_id=from_id,
                                to_fact_id=to_id,
                                relation_kind=relation_kind,
                                condition=None,
                                object_profile="debug",
                                evidence_source=f"src/{relation_kind}_{direction}.c:{index + 100}",
                                confidence=1.0,
                                payload={"note": "relative" * 10, "line": index},
                            )
                        )
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(facts, relatives)

            tool_result = open_facts_server(target).call_tool("detail", {"fact_id": "fact:target", "budget": "large"})

        content = tool_result.structured_content
        preview = content["relative_preview"]
        tool_serialized = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        # The real serialized response stays under the declared large ceiling.
        self.assertLessEqual(len(tool_serialized), BUDGETS["large"]["response_bytes"])
        self.assertTrue(preview["budget_exhausted"])
        self.assertEqual(preview["budget_exhausted_kind"], "response_bytes")
        self.assertEqual(preview["total_count"], len(relatives))
        self.assertTrue(any(bucket["truncated"] for bucket in preview["buckets"]))
        # dropped: response_bytes/limit/truncated + bucket_*_dropped_count (non-serialized) and mcp.detail log events

    def test_search_payload_preview_is_bounded_and_marks_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(
                [
                    FactRecord(
                        object_id="fact:wide",
                        object_name="Wide",
                        object_description="alpha wide payload",
                        object_source="src/wide.c:1",
                        object_profile="debug",
                        payload={"fact_kind": "function", **{f"k{index}": "value" * 3 for index in range(80)}},
                    )
                ]
            )

            response = open_facts_server(target).search("alpha", limit=1)

        self.assertEqual(response.result_count, 1)
        self.assertTrue(response.results[0].truncated)
        self.assertLessEqual(len(response.results[0].payload_preview), 8)
        self.assertNotIn("value" * 10, str(response.results[0].payload_preview))


if __name__ == "__main__":
    unittest.main()

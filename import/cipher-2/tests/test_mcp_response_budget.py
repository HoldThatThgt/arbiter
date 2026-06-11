import json
import tempfile
import unittest
from pathlib import Path

from cipher2.mcp import BUDGETS, open_mcp_server
from cipher2.storage import FactRecord, FactRelative, open_fact_store
from cipher2.tools.log import open_log


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
            server = open_mcp_server(target)

            small = server.detail("fact:budget", budget="small")
            normal = server.detail("fact:budget", budget="normal")
            large = server.detail("fact:budget", budget="large")

        self.assertTrue(small.payload_truncated)
        self.assertLessEqual(len(small.payload), 16)
        self.assertLess(len(small.source_context.lines), len(normal.source_context.lines))
        self.assertLess(len(normal.source_context.lines), len(large.source_context.lines))
        self.assertLessEqual(len(large.payload), 64)
        self.assertTrue(large.payload_truncated)
        self.assertFalse(normal.response_truncated)
        self.assertLessEqual(normal.response_bytes, normal.response_bytes_limit)

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

            server = open_mcp_server(target)
            response = server.detail("fact:target", budget="large")
            tool_result = server.call_tool("detail", {"fact_id": "fact:target", "budget": "large"})
            detail_event = next(
                event for event in open_log(target).read_events(channel="mcp").events
                if event.event_name == "mcp.detail"
            )

        serialized = json.dumps(response.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        tool_serialized = json.dumps(tool_result.structured_content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(serialized), BUDGETS["large"]["response_bytes"])
        self.assertLessEqual(len(tool_serialized), BUDGETS["large"]["response_bytes"])
        self.assertEqual(response.response_bytes, len(serialized))
        self.assertEqual(response.response_bytes_limit, BUDGETS["large"]["response_bytes"])
        self.assertTrue(response.response_truncated)
        self.assertTrue(response.relative_preview.budget_exhausted)
        self.assertEqual(response.relative_preview.budget_exhausted_kind, "response_bytes")
        self.assertEqual(response.to_json()["relative_preview"]["budget_exhausted_kind"], "response_bytes")
        self.assertGreater(response.relative_preview.bucket_relative_dropped_count, 0)
        self.assertGreater(response.relative_preview.bucket_dropped_count, 0)
        self.assertEqual(response.relative_preview.total_count, len(relatives))
        self.assertTrue(any(bucket.truncated for bucket in response.relative_preview.buckets))
        self.assertEqual(detail_event.counts["response_bytes"], len(tool_serialized))
        self.assertEqual(detail_event.counts["response_bytes_limit"], BUDGETS["large"]["response_bytes"])
        self.assertGreater(detail_event.counts["relative_bucket_dropped_count"], 0)
        self.assertEqual(detail_event.payload["budget_exhausted_kind"], "response_bytes")

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

            response = open_mcp_server(target).search("alpha", limit=1)

        self.assertEqual(response.result_count, 1)
        self.assertTrue(response.results[0].truncated)
        self.assertLessEqual(len(response.results[0].payload_preview), 8)
        self.assertNotIn("value" * 10, str(response.results[0].payload_preview))


if __name__ == "__main__":
    unittest.main()

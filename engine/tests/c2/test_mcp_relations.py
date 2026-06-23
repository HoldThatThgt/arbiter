# Migrated from cipher-2 tests/test_mcp_relations.py (M4 acceptance) — detail relative_preview spec.
# Adaptations: open_mcp_server -> open_facts_server; cipher2 -> arbiter_engine.facts.{store}; the
# "relations" tool rejection is an arbiter PROTOCOL error (RPCError -32601 tool_not_found); the
# cipher2.tools.log mcp.detail/mcp.relations event assertions are dropped (store runs log-disabled);
# the overlay-equivalence test publishes the overlay to disk and merges it via a reader query
# (Phase 2); the two missing-endpoint preview tests stay skipped because arbiter's store + overlay
# enforce endpoint closure, so a relative with a dangling endpoint is unreachable here.
import json
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.errors import RPCError
from arbiter_engine.facts.store import FactRecord, FactRelative, RelativeCondition, TemporaryOverlay, open_fact_store

from ._facts_server import open_facts_server
from .incremental_support import publish_overlay


def _fact(object_id: str, name: str, *, source: str = "src/main.c:1", profile: str = "default") -> FactRecord:
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=f"{name} function",
        object_source=source,
        object_profile=profile,
        payload={"fact_kind": "function"},
    )


def _relative(
    relative_id: str,
    from_id: str,
    to_id: str,
    *,
    relation_kind="direct_call",
    condition=None,
    evidence_source="src/main.c:4",
) -> FactRelative:
    return FactRelative(
        relative_id=relative_id,
        from_fact_id=from_id,
        to_fact_id=to_id,
        relation_kind=relation_kind,
        condition=condition,
        object_profile="default",
        evidence_source=evidence_source,
        confidence=1.0,
        payload={"line": 4},
    )


class _FakeFactView:
    view_state = "base"
    base_snapshot_id = "fake-snapshot"
    overlay_id = None

    def __init__(self, facts, relatives):
        self._facts = {fact.object_id: fact for fact in facts}
        self._relatives = list(relatives)

    def get_fact(self, object_id):
        return self._facts.get(object_id)

    def relatives_for_fact(self, fact_id, direction="both", relation_kind=None, limit=20):
        matches = [
            relative
            for relative in self._relatives
            if (relation_kind is None or relative.relation_kind == relation_kind)
            and (
                direction == "both"
                and (relative.from_fact_id == fact_id or relative.to_fact_id == fact_id)
                or direction == "incoming"
                and relative.to_fact_id == fact_id
                or direction == "outgoing"
                and relative.from_fact_id == fact_id
            )
        ]
        matches.sort(key=lambda relative: (relative.relation_kind, relative.from_fact_id, relative.to_fact_id, relative.relative_id))
        return matches[:limit]

    def count_relatives_for_fact(self, fact_id, direction="both", relation_kind=None):
        return len(self.relatives_for_fact(fact_id, direction=direction, relation_kind=relation_kind, limit=10_000))


class McpRelationsTest(unittest.TestCase):
    def test_relations_tool_call_is_not_public_mcp_interface(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RPCError) as cm:
                open_facts_server(Path(tmp)).call_tool("relations", {"fact_id": "fact:a", "direction": "outgoing"})
        self.assertEqual(cm.exception.code, -32601)
        self.assertEqual(cm.exception.data["kind"], "tool_not_found")

    def test_detail_preview_exposes_bounded_internal_relation_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            facts = [_fact("fact:a", "A"), _fact("fact:b", "B")]
            relatives = [
                _relative(
                    "rel:1",
                    "fact:a",
                    "fact:b",
                    condition=RelativeCondition(kind="branch", branch="then", source="src/main.c:4"),
                )
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(facts, relatives)

            detail = open_facts_server(target).call_tool("detail", {"fact_id": "fact:a"})

        self.assertFalse(detail.is_error)
        preview = detail.structured_content["relative_preview"]
        self.assertEqual(preview["relatives"][0]["relative_id"], "rel:1")
        self.assertEqual(preview["relatives"][0]["condition"]["branch"], "then")
        # dropped: open_log('mcp') mcp.relations event assertion (store log-disabled)

    def test_detail_preview_buckets_relatives_by_direction_and_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            facts = [_fact("fact:target", "target")]
            facts.extend(_fact(f"fact:caller:{index}", f"caller_{index}") for index in range(150))
            facts.extend(_fact(f"fact:reader:{index}", f"reader_{index}") for index in range(12))
            facts.extend(_fact(f"fact:callee:{index}", f"callee_{index}") for index in range(8))
            relatives = []
            relatives.extend(
                _relative(f"rel:caller:{index}", f"fact:caller:{index}", "fact:target")
                for index in range(150)
            )
            relatives.extend(
                _relative(
                    f"rel:reader:{index}",
                    f"fact:reader:{index}",
                    "fact:target",
                    relation_kind="field_read",
                )
                for index in range(12)
            )
            relatives.extend(
                _relative(f"rel:callee:{index}", "fact:target", f"fact:callee:{index}")
                for index in range(8)
            )
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(facts, relatives)

            detail = open_facts_server(target).call_tool("detail", {"fact_id": "fact:target", "budget": "normal"})

        self.assertFalse(detail.is_error)
        preview = detail.structured_content["relative_preview"]
        buckets = {bucket["bucket"]: bucket for bucket in preview["buckets"]}
        self.assertEqual(preview["incoming_counts"]["direct_call"], 150)
        self.assertEqual(preview["incoming_counts"]["field_read"], 12)
        self.assertEqual(preview["outgoing_counts"]["direct_call"], 8)
        self.assertEqual(preview["total_count"], 170)
        self.assertEqual(preview["shown_count"], 45)
        self.assertTrue(preview["truncated"])
        self.assertEqual(buckets["callers"]["total_count"], 150)
        self.assertEqual(buckets["callers"]["shown_count"], 25)
        self.assertTrue(buckets["callers"]["truncated"])
        self.assertEqual(buckets["field_readers"]["shown_count"], 12)
        self.assertFalse(buckets["field_readers"]["truncated"])
        self.assertEqual(buckets["callees"]["shown_count"], 8)
        self.assertGreater(len(preview["relatives"]), 5)
        bucket_relatives = [relative for bucket in preview["buckets"] for relative in bucket["relatives"]]
        self.assertLess(len(preview["relatives"]), len(bucket_relatives))
        self.assertNotEqual(
            json.dumps(preview["relatives"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(bucket_relatives, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    def test_detail_preview_exposes_type_fields_and_field_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            type_id = "fact:type:NullableDatum"
            field_id = "fact:field:NullableDatum:value"
            facts = [
                FactRecord(
                    object_id=type_id,
                    object_name="NullableDatum",
                    object_description="type NullableDatum",
                    object_source="include/fmgr.h:1",
                    object_profile="default",
                    payload={"fact_kind": "type"},
                ),
                FactRecord(
                    object_id=field_id,
                    object_name="value",
                    object_description="field value",
                    object_source="include/fmgr.h:1",
                    object_profile="default",
                    payload={"fact_kind": "field", "owner_name": "NullableDatum"},
                ),
            ]
            relatives = [_relative("rel:has-field:value", type_id, field_id, relation_kind="has_field")]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(facts, relatives)
            server = open_facts_server(target)

            type_detail = server.call_tool("detail", {"fact_id": type_id, "budget": "normal"})
            field_detail = server.call_tool("detail", {"fact_id": field_id, "budget": "normal"})

        self.assertFalse(type_detail.is_error)
        self.assertFalse(field_detail.is_error)
        type_buckets = {bucket["bucket"]: bucket for bucket in type_detail.structured_content["relative_preview"]["buckets"]}
        field_buckets = {bucket["bucket"]: bucket for bucket in field_detail.structured_content["relative_preview"]["buckets"]}
        self.assertEqual(type_buckets["fields"]["relatives"][0]["endpoint_name"], "value")
        self.assertEqual(field_buckets["field_owner"]["relatives"][0]["endpoint_name"], "NullableDatum")

    def test_detail_preview_high_fan_in_field_readers_keep_counts_and_diversity(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            field_id = "fact:field:list:length"
            facts = [_fact(field_id, "length", source="src/list.h:12", profile="field:length")]
            facts.extend(
                _fact(f"fact:reader:hot:{index:02d}", f"hot_{index:02d}", source=f"src/hot.c:{index + 1}")
                for index in range(28)
            )
            facts.extend(
                [
                    _fact("fact:reader:rare:b", "rare_b", source="src/b.c:1"),
                    _fact("fact:reader:rare:c", "rare_c", source="src/c.c:1"),
                ]
            )
            relatives = [
                _relative(
                    f"rel:field-read:hot:{index:02d}",
                    f"fact:reader:hot:{index:02d}",
                    field_id,
                    relation_kind="field_read",
                    evidence_source=f"src/hot.c:{100 + index}",
                )
                for index in range(28)
            ]
            relatives.extend(
                [
                    _relative(
                        "rel:field-read:rare:b",
                        "fact:reader:rare:b",
                        field_id,
                        relation_kind="field_read",
                        evidence_source="src/b.c:10",
                    ),
                    _relative(
                        "rel:field-read:rare:c",
                        "fact:reader:rare:c",
                        field_id,
                        relation_kind="field_read",
                        evidence_source="src/c.c:10",
                    ),
                ]
            )
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(facts, relatives)

            detail = open_facts_server(target).call_tool("detail", {"fact_id": field_id, "budget": "normal"})

        self.assertFalse(detail.is_error)
        preview = detail.structured_content["relative_preview"]
        field_readers = next(bucket for bucket in preview["buckets"] if bucket["bucket"] == "field_readers")
        names = [item["endpoint_name"] for item in field_readers["relatives"]]
        self.assertEqual(preview["incoming_counts"]["field_read"], 30)
        self.assertEqual(field_readers["total_count"], 30)
        self.assertEqual(field_readers["shown_count"], 25)
        self.assertTrue(field_readers["truncated"])
        self.assertEqual({item["relation_kind"] for item in field_readers["relatives"]}, {"field_read"})
        self.assertIn("rare_b", names)
        self.assertIn("rare_c", names)
        self.assertNotIn("hot_23", names)
        # dropped: event.counts["relative_diversity_bucket_count"] (log-only; diversity is observed via names)

    def test_detail_preview_rolls_up_call_sites_and_keeps_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            condition = RelativeCondition(kind="branch", branch="then", source="src/math.c:20")
            facts = [
                _fact("fact:add_var", "add_var", source="src/numeric.c:10"),
                _fact("fact:div_mod_var", "div_mod_var", source="src/math.c:1", profile="debug"),
                _fact("fact:caller_a", "caller_a", source="src/a.c:1"),
                _fact("fact:decl", "declared_add_var", source="src/decl.c:1"),
            ]
            relatives = [
                _relative("rel:div:1", "fact:div_mod_var", "fact:add_var", evidence_source="src/math.c:20"),
                _relative(
                    "rel:div:2",
                    "fact:div_mod_var",
                    "fact:add_var",
                    condition=condition,
                    evidence_source="src/math.c:21",
                ),
                _relative(
                    "rel:div:3",
                    "fact:div_mod_var",
                    "fact:add_var",
                    condition=condition,
                    evidence_source="src/math.c:22",
                ),
                _relative("rel:a", "fact:caller_a", "fact:add_var", evidence_source="src/a.c:8"),
                _relative("rel:def", "fact:decl", "fact:add_var", relation_kind="defines"),
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(facts, relatives)

            detail = open_facts_server(target).call_tool("detail", {"fact_id": "fact:add_var", "budget": "normal"})

        self.assertFalse(detail.is_error)
        preview = detail.structured_content["relative_preview"]
        buckets = {bucket["bucket"]: bucket for bucket in preview["buckets"]}
        callers = buckets["callers"]["relatives"]
        self.assertEqual(buckets["callers"]["total_count"], 4)
        self.assertEqual(buckets["callers"]["shown_count"], 2)
        div_mod = next(item for item in callers if item["endpoint_name"] == "div_mod_var")
        self.assertEqual(div_mod["instances"], 3)
        self.assertIsNone(div_mod["condition"])
        self.assertEqual(div_mod["conditions"], [condition.to_json()])
        self.assertEqual(div_mod["endpoint_source"], "src/math.c:1")
        self.assertEqual(div_mod["endpoint_profile"], "debug")
        self.assertEqual(preview["relatives"][0]["relation_kind"], "direct_call")
        self.assertEqual(buckets["incoming_defines"]["relatives"][0]["relation_kind"], "defines")
        # dropped: event.counts["relative_rollup_group_count"/"relative_collapsed_instance_count"] (log-only)

    def test_detail_preview_uses_source_diversity_under_tight_budget(self):
        # Re-authored from the original cipher-2 source-diversity / missing-endpoint test: the
        # missing-endpoint half is dropped (arbiter's store + overlay enforce endpoint closure, so a
        # dangling relative endpoint is unreachable here), but the source-diversity selection half is
        # restored with all-resolvable endpoints so that behavior stays covered. Six callers share
        # src/a.c; under the small budget (bucket_limit 5) the selector must apply the per-source soft
        # cap (2) and reach across distinct sources rather than greedily filling slots from src/a.c.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            facts = [_fact("fact:target", "target", source="src/target.c:1")]
            facts.extend(_fact(f"fact:a{index}", f"a{index}", source=f"src/a.c:{index + 1}") for index in range(6))
            facts.extend(
                [
                    _fact("fact:b0", "b0", source="src/b.c:1"),
                    _fact("fact:c0", "c0", source="src/c.c:1"),
                    _fact("fact:d0", "d0", source="generated"),
                    _fact("fact:z0", "z0", source="src/z.c:1"),
                ]
            )
            relatives = [
                _relative(f"rel:a{index}", f"fact:a{index}", "fact:target", evidence_source=f"src/a.c:{10 + index}")
                for index in range(6)
            ]
            relatives.extend(
                [
                    _relative("rel:b0", "fact:b0", "fact:target", evidence_source="src/b.c:10"),
                    _relative("rel:c0", "fact:c0", "fact:target", evidence_source="src/c.c:10"),
                    _relative("rel:d0", "fact:d0", "fact:target", evidence_source="generated:10"),
                    _relative("rel:z0", "fact:z0", "fact:target", evidence_source="src/z.c:10"),
                ]
            )
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(facts, relatives)

            detail = open_facts_server(target).call_tool("detail", {"fact_id": "fact:target", "budget": "small"})

        self.assertFalse(detail.is_error)
        preview = detail.structured_content["relative_preview"]
        callers = next(bucket for bucket in preview["buckets"] if bucket["bucket"] == "callers")
        names = [item["endpoint_name"] for item in callers["relatives"]]
        shown_sources = {item["endpoint_source"].rpartition(":")[0] or item["endpoint_source"] for item in callers["relatives"]}

        # All 10 callers count; only 5 fit the small budget, so the bucket is truncated.
        self.assertEqual(preview["incoming_counts"]["direct_call"], 10)
        self.assertEqual(callers["total_count"], 10)
        self.assertEqual(callers["shown_count"], 5)
        self.assertTrue(callers["truncated"])
        # Diversity, not greedy name order: src/a.c is held to the soft cap (2) instead of filling all
        # five slots, so the shown set reaches b0/c0/d0 (which sort after a2..a5 by endpoint name).
        self.assertEqual(names, ["a0", "a1", "b0", "c0", "d0"])
        self.assertEqual(sum(1 for name in names if name.startswith("a")), 2)
        self.assertEqual(shown_sources, {"src/a.c", "src/b.c", "src/c.c", "generated"})
        # z0 falls off under the tight budget once the diverse slots are filled.
        self.assertNotIn("z0", names)

    @unittest.skip("dangling relative endpoints are unreachable in arbiter — store + overlay enforce endpoint closure")
    def test_detail_preview_missing_endpoint_profile_is_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            facts = [_fact("fact:target", "target", source="src/target.c:1")]
            relatives = [_relative("rel:missing", "fact:missing", "fact:target", evidence_source="src/missing.c:10")]
            server = open_facts_server(target, fact_view_provider=lambda: _FakeFactView(facts, relatives))
            detail = server.call_tool("detail", {"fact_id": "fact:target", "budget": "normal"})
        self.assertFalse(detail.is_error)

    def test_detail_preview_rollup_and_selection_are_identical_for_overlay_view(self):
        facts = [
            _fact("fact:target", "target", source="src/target.c:1"),
            _fact("fact:caller", "caller", source="src/caller.c:1", profile="debug"),
            _fact("fact:other", "other", source="src/other.c:1", profile="release"),
        ]
        relatives = [
            _relative("rel:caller:1", "fact:caller", "fact:target", evidence_source="src/caller.c:10"),
            _relative("rel:caller:2", "fact:caller", "fact:target", evidence_source="src/caller.c:11"),
            _relative("rel:other", "fact:other", "fact:target", evidence_source="src/other.c:10"),
        ]
        with tempfile.TemporaryDirectory() as base_tmp, tempfile.TemporaryDirectory() as overlay_tmp:
            base_target = Path(base_tmp)
            open_fact_store(base_target, mode="w", log_enabled=False).replace_snapshot(facts, relatives)
            base_detail = open_facts_server(base_target).call_tool("detail", {"fact_id": "fact:target", "budget": "normal"})
            overlay_target = Path(overlay_tmp)
            store = open_fact_store(overlay_target, mode="w", log_enabled=False)
            manifest = store.replace_snapshot([facts[0]], [])
            overlay = TemporaryOverlay(
                overlay_id="overlay-rel-preview",
                view_state="overlay",
                fact_upserts=facts[1:],
                relative_upserts=relatives,
            )
            # Publish the overlay to disk; a reader query merges base + overlay (Phase 2).
            publish_overlay(overlay_target, overlay, base_snapshot_id=manifest.snapshot_id)
            overlay_server = open_facts_server(overlay_target, role="QUERY", seat="executor")
            overlay_detail = overlay_server.call_tool("detail", {"fact_id": "fact:target", "budget": "normal"})
        self.assertFalse(base_detail.is_error)
        self.assertFalse(overlay_detail.is_error)
        # The overlay-merged preview is identical to the same facts published as one base snapshot.
        self.assertEqual(overlay_detail.structured_content["relative_preview"], base_detail.structured_content["relative_preview"])


if __name__ == "__main__":
    unittest.main()

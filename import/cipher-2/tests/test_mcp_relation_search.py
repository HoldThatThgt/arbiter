import tempfile
import unittest
from pathlib import Path

from cipher2.mcp import open_mcp_server
from cipher2.storage import FactRecord, FactRelative, RelativeCondition, TemporaryOverlay, open_fact_store
from cipher2.tools.log import open_log


def _fact(object_id: str, name: str, *, kind: str = "function", source: str = "src/main.c:1", **payload) -> FactRecord:
    data = {"fact_kind": kind}
    data.update(payload)
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=f"{name} {kind}",
        object_source=source,
        object_profile="default",
        payload=data,
    )


def _relative(
    relative_id: str,
    from_id: str,
    to_id: str,
    *,
    kind: str = "field_read",
    condition=None,
) -> FactRelative:
    return FactRelative(
        relative_id=relative_id,
        from_fact_id=from_id,
        to_fact_id=to_id,
        relation_kind=kind,
        condition=condition,
        object_profile="default",
        evidence_source="src/main.c:10",
        confidence=1.0,
        payload={"line": 10},
    )


class McpRelationSearchTest(unittest.TestCase):
    def test_readers_file_filter_strips_endpoint_line_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            field = _fact("fact:field:NullableDatum:value", "value", kind="field", source="src/fmgr.h:7", owner_name="NullableDatum")
            readers = [
                _fact("fact:function:ruleutils", "rule_get_expr", source="src/backend/utils/adt/ruleutils.c:9647"),
                _fact("fact:function:numeric", "numeric_add", source="src/backend/utils/adt/numeric.c:120"),
            ]
            relatives = [
                _relative("rel:read:ruleutils", readers[0].object_id, field.object_id),
                _relative("rel:read:numeric", readers[1].object_id, field.object_id),
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([field, *readers], relatives)

            response = open_mcp_server(target).search("readers:NullableDatum.value file:ruleutils.c", limit=20)

        self.assertEqual(response.status, "ok")
        self.assertEqual(response.total, 1)
        self.assertEqual([item.object_name for item in response.results], ["rule_get_expr"])
        self.assertEqual(response.results[0].relation_kind, "field_read")
        self.assertEqual(response.query_kind, "relation")
        self.assertTrue(response.complete)
        self.assertFalse(response.budget_exhausted)

    def test_relation_search_too_broad_is_keyed_by_limit_not_fixed_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            field = _fact("fact:field:List:length", "length", kind="field", source="src/list.h:12", owner_name="List")
            readers = [
                _fact(f"fact:function:reader:{index:02d}", f"reader_{index:02d}", source=f"src/readers.c:{index + 1}")
                for index in range(30)
            ]
            relatives = [
                _relative(f"rel:read:{index:02d}", reader.object_id, field.object_id)
                for index, reader in enumerate(readers)
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([field, *readers], relatives)

            result = open_mcp_server(target).call_tool("search", {"query": "readers:List.length", "limit": 20})
            event = next(
                event for event in open_log(target).read_events(channel="mcp").events
                if event.event_name == "mcp.search"
            )

        self.assertFalse(result.is_error)
        content = result.structured_content
        self.assertEqual(content["status"], "too_broad")
        self.assertEqual(content["total"], 30)
        self.assertEqual(content["matched_endpoint_count"], 30)
        self.assertEqual(content["result_count"], 20)
        self.assertTrue(content["truncated"])
        self.assertTrue(content["complete"])
        self.assertFalse(content["budget_exhausted"])
        self.assertTrue(content["total_is_exact"])
        self.assertIn("file:<path>", content["available_filters"])
        self.assertIn("search('readers:fact:field:List:length file:<path>')", content["examples"])
        self.assertNotIn("top_by_salience", content)
        self.assertEqual(event.payload["returned_ids"], [row["object_id"] for row in content["results"]])
        self.assertEqual(len(event.payload["returned_ids"]), 20)
        self.assertNotIn("query_sha256", event.payload)
        self.assertEqual(
            set(content["results"][0]),
            {
                "object_id",
                "object_name",
                "object_source",
                "relation_kind",
                "instances",
                "representative_relative_id",
                "hop",
            },
        )

    def test_file_scoped_too_broad_returns_bounded_answer_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            field = _fact("fact:field:List:length", "length", kind="field", source="src/list.h:12", owner_name="List")
            readers = [
                _fact(f"fact:function:reader:{index:02d}", f"reader_{index:02d}", source=f"src/numeric.c:{index + 1}")
                for index in range(30)
            ]
            relatives = [
                _relative(f"rel:read:{index:02d}", reader.object_id, field.object_id)
                for index, reader in enumerate(readers)
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([field, *readers], relatives)

            result = open_mcp_server(target).call_tool(
                "search",
                {"query": "readers:List.length file:numeric.c", "limit": 20},
            )

        self.assertFalse(result.is_error)
        content = result.structured_content
        self.assertEqual(content["status"], "too_broad")
        self.assertEqual(content["total"], 30)
        self.assertEqual(content["matched_endpoint_count"], 30)
        self.assertTrue(content["complete"])
        self.assertFalse(content["budget_exhausted"])
        self.assertEqual(content.get("available_filters", []), [])
        self.assertEqual(content.get("examples", []), [])
        self.assertIn("most salient subset", content["message"])
        self.assertIn("Report the returned subset with the total", content["message"])
        self.assertNotIn("name:", content["message"])
        self.assertNotIn("caller:", content["message"])
        self.assertNotIn("name:", str(content.get("available_filters", [])))
        self.assertNotIn("caller:", str(content.get("available_filters", [])))
        self.assertNotIn("name:", str(content.get("examples", [])))
        self.assertNotIn("caller:", str(content.get("examples", [])))

    def test_empty_writers_guides_to_accessors_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            field = _fact("fact:field:List:length", "length", kind="field", source="src/list.h:12", owner_name="List")
            reader = _fact("fact:function:list_length", "list_length", source="src/list.c:42")
            relatives = [_relative("rel:read:length", reader.object_id, field.object_id, kind="field_read")]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([field, reader], relatives)
            server = open_mcp_server(target)

            writers = server.call_tool("search", {"query": f"writers:{field.object_id}", "limit": 20})
            accessors = server.search(f"accessors:{field.object_id}", limit=20)

        self.assertFalse(writers.is_error)
        content = writers.structured_content
        self.assertEqual(content["status"], "ok")
        self.assertEqual(content["total"], 0)
        self.assertEqual(content["matched_endpoint_count"], 0)
        self.assertEqual(content["result_count"], 0)
        self.assertIn(f"accessors:{field.object_id}", content["message"])
        self.assertIn(f"detail({field.object_id})", content["message"])
        self.assertIn(f"accessors:{field.object_id}", content["examples"])
        self.assertEqual(accessors.status, "ok")
        self.assertEqual([item.object_id for item in accessors.results], [reader.object_id])

    def test_callers_callees_and_caller_name_synonyms_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            add_var = _fact("fact:function:add_var", "add_var", source="src/math.c:10")
            numeric_add = _fact("fact:function:numeric_add", "numeric_add", source="src/numeric.c:20")
            other = _fact("fact:function:other_add", "other_add", source="src/other.c:30")
            relatives = [
                _relative("rel:call:numeric", numeric_add.object_id, add_var.object_id, kind="direct_call"),
                _relative("rel:call:other", other.object_id, add_var.object_id, kind="direct_call"),
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([add_var, numeric_add, other], relatives)
            server = open_mcp_server(target)

            caller_filter = server.search("callers:add_var caller:numeric_add", limit=20)
            name_filter = server.search("callers:add_var name:numeric_add", limit=20)
            callees = server.search("callees:numeric_add", limit=20)

        self.assertEqual(caller_filter.status, "ok")
        self.assertEqual([item.object_id for item in caller_filter.results], ["fact:function:numeric_add"])
        self.assertEqual([item.object_id for item in caller_filter.results], [item.object_id for item in name_filter.results])
        self.assertEqual([item.object_id for item in callees.results], ["fact:function:add_var"])

    def test_relation_search_transitive_uses_slim_rows_and_no_salience_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "root", source="src/root.c:1")
            middle = _fact("fact:function:middle", "middle", source="src/middle.c:2")
            leaf = _fact("fact:function:leaf", "leaf", source="src/leaf.c:3")
            relatives = [
                _relative("rel:root:middle", root.object_id, middle.object_id, kind="direct_call"),
                _relative("rel:middle:leaf", middle.object_id, leaf.object_id, kind="direct_call"),
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([root, middle, leaf], relatives)

            result = open_mcp_server(target).call_tool("search", {"query": "callees:root depth:2", "limit": 20})

        self.assertFalse(result.is_error)
        content = result.structured_content
        self.assertEqual(content["status"], "ok")
        self.assertEqual(content["query_kind"], "relation_transitive")
        self.assertEqual(content["matched_endpoint_count"], 2)
        self.assertEqual(content["result_count"], 2)
        self.assertTrue(content["complete"])
        self.assertFalse(content["budget_exhausted"])
        self.assertNotIn("top_by_salience", content)
        self.assertEqual(
            content["results"],
            [
                {
                    "object_id": "fact:function:middle",
                    "object_name": "middle",
                    "object_source": "src/middle.c:2",
                    "relation_kind": "direct_call",
                    "instances": 1,
                    "representative_relative_id": "rel:root:middle",
                    "hop": 1,
                },
                {
                    "object_id": "fact:function:leaf",
                    "object_name": "leaf",
                    "object_source": "src/leaf.c:3",
                    "relation_kind": "direct_call",
                    "instances": 1,
                    "representative_relative_id": "rel:middle:leaf",
                    "hop": 2,
                },
            ],
        )

    def test_reachable_returns_path_complete_and_no_endpoint_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "root", source="src/root.c:1")
            middle = _fact("fact:function:middle", "middle", source="src/middle.c:2")
            leaf = _fact("fact:function:leaf", "leaf", source="src/leaf.c:3")
            relatives = [
                _relative("rel:root:middle", root.object_id, middle.object_id, kind="direct_call"),
                _relative("rel:middle:leaf", middle.object_id, leaf.object_id, kind="direct_call"),
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([root, middle, leaf], relatives)

            result = open_mcp_server(target).call_tool("search", {"query": "reachable:root->leaf", "limit": 20})

        self.assertFalse(result.is_error)
        content = result.structured_content
        self.assertEqual(content["status"], "ok")
        self.assertEqual(content["query_kind"], "relation_reachable")
        self.assertEqual(content["result_count"], 0)
        self.assertEqual(content["results"], [])
        self.assertTrue(content["reachable"])
        self.assertTrue(content["complete"])
        self.assertFalse(content["budget_exhausted"])
        self.assertEqual([node["object_name"] for node in content["path"]], ["root", "middle", "leaf"])
        self.assertEqual([node["hop"] for node in content["path"]], [0, 1, 2])

    def test_reachable_path_serializes_hop_conditions_and_no_hit_keeps_empty_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "root", source="src/root.c:1")
            unguarded = _fact("fact:function:unguarded", "unguarded", source="src/unguarded.c:2")
            branched = _fact("fact:function:branched", "branched", source="src/branched.c:3")
            looped = _fact("fact:function:looped", "looped", source="src/looped.c:4")
            leaf = _fact("fact:function:leaf", "leaf", source="src/leaf.c:5")
            missing = _fact("fact:function:missing", "missing", source="src/missing.c:6")
            branch_condition = RelativeCondition(kind="branch", expression="reset_flag", branch="then", source="src/root.c:4")
            loop_condition = RelativeCondition(kind="loop_guard", expression="i < n", branch="body", source="src/branched.c:8")
            compile_condition = RelativeCondition(kind="compile_guard", expression="FEATURE_X", branch="then", source="src/looped.c:1")
            relatives = [
                _relative("rel:root:unguarded", root.object_id, unguarded.object_id, kind="direct_call"),
                _relative(
                    "rel:unguarded:branched",
                    unguarded.object_id,
                    branched.object_id,
                    kind="direct_call",
                    condition=branch_condition,
                ),
                _relative(
                    "rel:branched:looped",
                    branched.object_id,
                    looped.object_id,
                    kind="direct_call",
                    condition=loop_condition,
                ),
                _relative(
                    "rel:looped:leaf",
                    looped.object_id,
                    leaf.object_id,
                    kind="direct_call",
                    condition=compile_condition,
                ),
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [root, unguarded, branched, looped, leaf, missing],
                relatives,
            )
            server = open_mcp_server(target)

            hit = server.call_tool("search", {"query": "reachable:root->leaf", "limit": 20})
            no_hit = server.call_tool("search", {"query": "reachable:root->missing", "limit": 20})

        self.assertFalse(hit.is_error)
        path = hit.structured_content["path"]
        self.assertEqual([node["object_name"] for node in path], ["root", "unguarded", "branched", "looped", "leaf"])
        self.assertNotIn("condition", path[0])
        self.assertNotIn("condition", path[1])
        self.assertEqual(path[2]["condition"], branch_condition.to_json())
        self.assertEqual(path[3]["condition"], loop_condition.to_json())
        self.assertEqual(path[4]["condition"], compile_condition.to_json())
        self.assertFalse(no_hit.is_error)
        self.assertFalse(no_hit.structured_content["reachable"])
        self.assertEqual(no_hit.structured_content.get("path", []), [])

    def test_reachable_path_can_cross_dispatch_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "root", source="src/root.c:1")
            leaf = _fact("fact:function:leaf", "leaf", source="src/leaf.c:3")
            slot = _fact("fact:field:ExecProcNode", "ExecProcNode", kind="field", source="src/exec.h:327")
            relatives = [
                _relative("rel:root:slot", root.object_id, slot.object_id, kind="dispatches_via"),
                _relative("rel:slot:leaf", slot.object_id, leaf.object_id, kind="assigned_to"),
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([root, leaf, slot], relatives)

            result = open_mcp_server(target).call_tool("search", {"query": "reachable:root->leaf", "limit": 20})

        self.assertFalse(result.is_error)
        content = result.structured_content
        self.assertTrue(content["reachable"])
        self.assertEqual([node["object_name"] for node in content["path"]], ["root", "leaf"])
        self.assertEqual(content["path"][1]["relation_kind"], "dispatches_via")
        self.assertEqual(content["path"][1]["representative_relative_id"], "rel:root:slot")

    def test_relation_search_invalid_depth_returns_refinement_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "root", source="src/root.c:1")
            leaf = _fact("fact:function:leaf", "leaf", source="src/leaf.c:3")
            relatives = [_relative("rel:root:leaf", root.object_id, leaf.object_id, kind="direct_call")]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([root, leaf], relatives)

            result = open_mcp_server(target).call_tool("search", {"query": "callees:root depth:0", "limit": 20})

        self.assertFalse(result.is_error)
        content = result.structured_content
        self.assertEqual(content["status"], "needs_refinement")
        self.assertEqual(content["query_kind"], "relation_transitive")
        self.assertFalse(content["complete"])
        self.assertIn("depth", content["message"])
        self.assertIn("callees:root depth:2", content["examples"])

    def test_relation_search_hard_error_tool_result_keeps_storage_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = open_mcp_server(Path(tmp)).call_tool(
                "search",
                {"query": "readers:fact:field:slot condition:branch", "limit": 20},
            )

        self.assertTrue(result.is_error)
        error = result.structured_content["error"]
        self.assertEqual(error["code"], "invalid_query")
        self.assertEqual(error["details"]["storage_code"], "invalid_relation_query")
        self.assertIn("detail(<fact_id>)", error["message"])
        self.assertIn("`condition` field", error["message"])
        self.assertNotIn("Traceback", error["message"])

    def test_unique_exact_function_anchor_ignores_substring_fallback_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            lock_acquire = _fact(
                "fact:function:lwlock_acquire",
                "LWLockAcquire",
                source="src/backend/storage/lmgr/lwlock.c:1150",
            )
            lock_acquire_or_wait = _fact(
                "fact:function:lwlock_acquire_or_wait",
                "LWLockAcquireOrWait",
                source="src/backend/storage/lmgr/lwlock.c:1320",
            )
            caller = _fact(
                "fact:function:buffer_alloc",
                "BufferAlloc",
                source="src/backend/storage/buffer/bufmgr.c:900",
            )
            relatives = [
                _relative(
                    "rel:call:lwlock_acquire",
                    caller.object_id,
                    lock_acquire.object_id,
                    kind="direct_call",
                )
            ]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [lock_acquire, lock_acquire_or_wait, caller],
                relatives,
            )

            response = open_mcp_server(target).search("callers:LWLockAcquire", limit=20)

        self.assertEqual(response.status, "ok")
        self.assertEqual(response.total, 1)
        self.assertEqual(response.anchor.object_id, lock_acquire.object_id)
        self.assertEqual(response.anchor_candidates, [])
        self.assertEqual([item.object_id for item in response.results], [caller.object_id])

    def test_ambiguous_anchor_returns_deterministically_ordered_refinement(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            add_a = _fact("fact:function:add:a", "add_var", source="src/a.c:1")
            add_b = _fact("fact:function:add:b", "add_var", source="src/b.c:1")
            caller = _fact("fact:function:caller", "caller", source="src/caller.c:1")
            relatives = [_relative("rel:call:a", caller.object_id, add_a.object_id, kind="direct_call")]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([add_b, caller, add_a], relatives)

            response = open_mcp_server(target).search("callers:add_var", limit=20)

        self.assertEqual(response.status, "needs_refinement")
        self.assertEqual(response.result_count, 0)
        self.assertEqual([candidate.object_id for candidate in response.anchor_candidates], ["fact:function:add:a", "fact:function:add:b"])
        self.assertIn("object_id=fact:function:add:a", response.message)
        self.assertIn("source=src/a.c:1", response.message)
        self.assertIn("search('callers:fact:function:add:a')", response.examples)

    def test_field_anchor_refinement_lists_candidate_id_owner_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            foo_value = _fact("fact:field:Foo:value", "value", kind="field", source="include/foo.h:3", owner_name="Foo")
            bar_value = _fact("fact:field:Bar:value", "value", kind="field", source="include/bar.h:5", owner_name="Bar")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([bar_value, foo_value], [])

            response = open_mcp_server(target).search("writers:value", limit=20)

        self.assertEqual(response.status, "needs_refinement")
        self.assertEqual([candidate.object_id for candidate in response.anchor_candidates], [bar_value.object_id, foo_value.object_id])
        self.assertIn(f"object_id={bar_value.object_id}", response.message)
        self.assertIn("owner=Bar", response.message)
        self.assertIn("source=include/bar.h:5", response.message)
        self.assertEqual(response.anchor_candidates[0].payload_preview["anchor_owner"], "Bar")
        self.assertIn(f"search('writers:{bar_value.object_id}')", response.examples)

    def test_single_fuzzy_anchor_requires_refinement_instead_of_joining_wrong_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            init_var_from_num = _fact("fact:function:init_var_from_num", "init_var_from_num", source="src/numeric.c:7211")
            caller = _fact("fact:function:numeric_add", "numeric_add", source="src/numeric.c:1000")
            relatives = [_relative("rel:call:init_var_from_num", caller.object_id, init_var_from_num.object_id, kind="direct_call")]
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [init_var_from_num, caller],
                relatives,
            )

            response = open_mcp_server(target).search("callers:init_var", limit=20)

        self.assertEqual(response.status, "needs_refinement")
        self.assertEqual(response.result_count, 0)
        self.assertEqual(response.results, [])
        self.assertIsNone(response.anchor)
        self.assertIn("No exact function anchor matched `init_var`", response.message)
        self.assertEqual([candidate.object_name for candidate in response.anchor_candidates], ["init_var_from_num"])
        self.assertEqual(response.anchor_candidates[0].payload_preview["resolution_tier"], 3)
        self.assertEqual(response.anchor_candidates[0].payload_preview["anchor_match"], "fuzzy")

    def test_relation_search_sees_overlay_facts_and_relatives(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            field = _fact("fact:field:List:length", "length", kind="field", source="src/list.h:12", owner_name="List")
            base_reader = _fact("fact:function:base_reader", "base_reader", source="src/base.c:1")
            overlay_reader = _fact("fact:function:overlay_reader", "overlay_reader", source="src/overlay.c:1")
            base_relative = _relative("rel:base", base_reader.object_id, field.object_id)
            overlay_relative = _relative("rel:overlay", overlay_reader.object_id, field.object_id)
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([field, base_reader], [base_relative])
            overlay = TemporaryOverlay(
                overlay_id="relation-search",
                fact_upserts=[overlay_reader],
                relative_upserts=[overlay_relative],
            )
            server = open_mcp_server(target, fact_view_provider=lambda: store.open_view(overlay))

            response = server.search("readers:List.length file:overlay.c", limit=20)

        self.assertEqual(response.status, "ok")
        self.assertEqual([item.object_id for item in response.results], ["fact:function:overlay_reader"])


if __name__ == "__main__":
    unittest.main()

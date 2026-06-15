# Migrated from cipher-2 tests/test_storage_relative_store.py (M4 acceptance — imports rewritten cipher2.*->arbiter_engine.facts.*, .cipher->.arbiter/facts).
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import arbiter_engine.facts.store as storage_module
from arbiter_engine.facts.store import FactRecord, FactRelative, RelativeCondition, StorageError, open_fact_store


def _fact(object_id: str, kind: str, name: str) -> FactRecord:
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=f"{kind} {name}",
        object_source=f"src/{name}.c:1",
        object_profile="debug",
        payload={"fact_kind": kind},
    )


def _relative(
    relative_id: str,
    from_fact_id: str,
    to_fact_id: str,
    relation_kind: str,
    *,
    condition=None,
) -> FactRelative:
    return FactRelative(
        relative_id=relative_id,
        from_fact_id=from_fact_id,
        to_fact_id=to_fact_id,
        relation_kind=relation_kind,
        condition=condition,
        object_profile="debug",
        evidence_source="src/main.c:10",
        confidence=1.0,
        payload={"source": "test"},
    )


def _facts():
    return [
        _fact("fact:file:a", "code_file", "a"),
        _fact("fact:file:b", "code_file", "b"),
        _fact("fact:function:main", "function", "main"),
        _fact("fact:function:read_a", "function", "read_a"),
        _fact("fact:slot:ops.read", "field", "ops_read"),
        _fact("fact:type:ops", "type", "ops"),
    ]


class StorageRelativeStoreTest(unittest.TestCase):
    def test_replace_snapshot_writes_v5_gzip_relatives_manifest_stats_index_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            condition = RelativeCondition(kind="branch", expression="a", branch="then", source="src/main.c:9")
            relatives = [
                _relative("rel:include", "fact:file:a", "fact:file:b", "include"),
                _relative("rel:call", "fact:function:main", "fact:function:read_a", "direct_call"),
                _relative("rel:assign", "fact:slot:ops.read", "fact:function:read_a", "assigned_to", condition=condition),
                _relative("rel:dispatch", "fact:function:main", "fact:slot:ops.read", "dispatches_via"),
                _relative("rel:field", "fact:type:ops", "fact:slot:ops.read", "has_field"),
                _relative("rel:field_read", "fact:function:main", "fact:slot:ops.read", "field_read"),
                _relative("rel:field_write", "fact:function:main", "fact:slot:ops.read", "field_write"),
            ]

            manifest = open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(_facts(), relatives)

            snapshot_dir = target / ".arbiter" / "facts" / "snapshots" / manifest.snapshot_id
            self.assertEqual(manifest.schema_version, 5)
            self.assertEqual(manifest.snapshot_format, "compact-jsonl-gzip")
            self.assertEqual(manifest.compression, "gzip-1")
            self.assertEqual(manifest.fact_count, 6)
            self.assertEqual(manifest.relative_count, 7)
            self.assertEqual(manifest.read_index["fact_count"], 6)
            self.assertEqual(manifest.read_index["relative_count"], 7)
            self.assertGreater(manifest.read_index["bytes_on_disk"], 0)
            self.assertRegex(manifest.relatives_sha256, r"^[0-9a-f]{64}$")
            self.assertTrue((snapshot_dir / "facts.jsonl.gz").exists())
            self.assertTrue((snapshot_dir / "relatives.jsonl.gz").exists())
            self.assertTrue((snapshot_dir / "read_index.sqlite").exists())
            self.assertFalse((snapshot_dir / "facts.jsonl").exists())
            self.assertFalse((snapshot_dir / "relatives.jsonl").exists())
            self.assertFalse((snapshot_dir / "read_index.sqlite-wal").exists())
            self.assertFalse((snapshot_dir / "read_index.sqlite-shm").exists())
            self.assertFalse((snapshot_dir / "read_index.sqlite-journal").exists())
            self.assertFalse((snapshot_dir / "graph_objects.jsonl").exists())
            self.assertFalse((snapshot_dir / "graph_relatives.jsonl").exists())
            self.assertFalse((snapshot_dir / "graph_derived_from.jsonl").exists())
            manifest_json = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
            stats_json = json.loads((snapshot_dir / "stats.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest_json["schema_version"], 5)
            self.assertEqual(manifest_json["snapshot_format"], "compact-jsonl-gzip")
            self.assertEqual(manifest_json["compression"], "gzip-1")
            self.assertEqual(manifest_json["read_index"]["schema_version"], 6)
            self.assertEqual(manifest_json["read_index"]["projection_kind"], "proxy-key-column-projection")
            self.assertGreater(manifest_json["file_bytes"]["relatives"]["raw_bytes"], 0)
            self.assertGreater(manifest_json["file_bytes"]["relatives"]["compressed_bytes"], 0)
            self.assertEqual(manifest_json["stats"], stats_json)
            self.assertEqual(stats_json["total_relatives"], 7)
            self.assertEqual(stats_json["snapshot_format"], "compact-jsonl-gzip")
            self.assertEqual(stats_json["compression"], "gzip-1")
            self.assertEqual(stats_json["read_index_state"], "ready")
            self.assertEqual(stats_json["read_index_bytes"], manifest_json["read_index"]["bytes_on_disk"])
            self.assertEqual(stats_json["file_bytes"], manifest_json["file_bytes"])
            self.assertEqual(stats_json["conditional_relative_count"], 1)
            self.assertEqual(stats_json["relation_kinds"]["assigned_to"], 1)
            self.assertEqual(stats_json["relation_kinds"]["field_read"], 1)
            self.assertEqual(stats_json["relation_kinds"]["field_write"], 1)
            with sqlite3.connect(snapshot_dir / "read_index.sqlite") as connection:
                self.assertEqual(
                    [row[1] for row in connection.execute("PRAGMA table_info(fact_keys)")],
                    ["fact_k", "object_id"],
                )
                relatives_columns = [row[1] for row in connection.execute("PRAGMA table_info(relatives)")]
                self.assertEqual(
                    relatives_columns,
                    [
                        "relative_k",
                        "from_k",
                        "to_k",
                        "relation_kind_code",
                        "confidence",
                        "object_profile",
                        "evidence_source",
                        "condition_json",
                        "payload_json",
                    ],
                )
                self.assertEqual(
                    [row[1] for row in connection.execute("PRAGMA table_info(relative_ids)")],
                    ["relative_k", "relative_id"],
                )
                self.assertEqual(
                    connection.execute("SELECT typeof(relative_k), typeof(from_k), typeof(to_k) FROM relatives LIMIT 1").fetchone(),
                    ("integer", "integer", "integer"),
                )
                self.assertEqual(
                    connection.execute("SELECT typeof(fact_k) FROM fact_keys LIMIT 1").fetchone()[0],
                    "integer",
                )
                self.assertEqual(
                    [row[0] for row in connection.execute("SELECT object_id FROM fact_keys ORDER BY fact_k")],
                    sorted(fact.object_id for fact in _facts()),
                )
                self.assertEqual(
                    [row[0] for row in connection.execute("SELECT relative_id FROM relative_ids ORDER BY relative_k")],
                    sorted(relative.relative_id for relative in relatives),
                )

    def test_replace_snapshot_sorted_unique_validates_relative_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            facts = sorted(_facts(), key=lambda fact: fact.object_id)
            relatives = [
                _relative("rel:call", "fact:function:main", "fact:function:read_a", "direct_call"),
                _relative("rel:include", "fact:file:a", "fact:file:b", "include"),
            ]
            store = open_fact_store(target, mode="w", log_enabled=False)

            manifest = store.replace_snapshot_sorted_unique(facts, relatives, [])

            self.assertEqual(manifest.fact_count, 6)
            self.assertEqual(manifest.relative_count, 2)
            self.assertEqual([relative.relative_id for relative in store.iter_relatives()], ["rel:call", "rel:include"])

            with self.assertRaises(StorageError) as missing:
                store.replace_snapshot_sorted_unique(
                    facts,
                    [_relative("rel:missing", "fact:missing", "fact:file:b", "include")],
                    [],
                )
            self.assertEqual(missing.exception.code, "relative_endpoint_missing")

    def test_iter_relatives_stats_and_relations_query_current_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            condition = RelativeCondition(kind="branch", expression="a", branch="then", source="src/main.c:9")
            relatives = [
                _relative("rel:dispatch", "fact:function:main", "fact:slot:ops.read", "dispatches_via"),
                _relative("rel:call", "fact:function:main", "fact:function:read_a", "direct_call"),
                _relative("rel:assign", "fact:slot:ops.read", "fact:function:read_a", "assigned_to", condition=condition),
            ]
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot(_facts(), relatives)

            self.assertEqual([relative.relative_id for relative in store.iter_relatives()], ["rel:assign", "rel:call", "rel:dispatch"])
            outgoing = store.relatives_for_fact("fact:function:main", direction="outgoing", limit=20)
            self.assertEqual([relative.relative_id for relative in outgoing], ["rel:call", "rel:dispatch"])
            self.assertEqual(store.count_relatives_for_fact("fact:function:main", direction="outgoing"), 2)
            incoming = store.relatives_for_fact("fact:function:read_a", direction="incoming", limit=20)
            self.assertEqual([relative.relative_id for relative in incoming], ["rel:assign", "rel:call"])
            self.assertEqual(store.count_relatives_for_fact("fact:function:read_a", direction="incoming"), 2)
            assigned = store.relatives_for_fact(
                "fact:function:read_a",
                direction="incoming",
                relation_kind="assigned_to",
                limit=20,
            )
            self.assertEqual([relative.relative_id for relative in assigned], ["rel:assign"])
            self.assertEqual(
                store.count_relatives_for_fact(
                    "fact:function:read_a",
                    direction="incoming",
                    relation_kind="assigned_to",
                ),
                1,
            )
            self.assertEqual(assigned[0].condition.expression, "a")
            stats = store.stats()
            self.assertEqual(stats.total_relatives, 3)
            self.assertEqual(stats.relation_kinds, {"assigned_to": 1, "direct_call": 1, "dispatches_via": 1})
            self.assertEqual(stats.conditional_relative_count, 1)
            self.assertEqual(stats.orphan_relative_count, 0)

    def test_relation_search_single_text_fallback_anchor_needs_refinement(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            init_var_from_num = _fact("fact:function:init_var_from_num", "function", "init_var_from_num")
            caller = _fact("fact:function:numeric_add", "function", "numeric_add")
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot(
                [init_var_from_num, caller],
                [_relative("rel:call:init_var_from_num", caller.object_id, init_var_from_num.object_id, "direct_call")],
            )

            result = store.relation_search("callers:init_var", limit=20)

        self.assertEqual(result.status, "needs_refinement")
        self.assertEqual(result.total, 0)
        self.assertEqual(result.matches, ())
        self.assertIsNone(result.anchor)
        self.assertEqual(len(result.anchor_candidates), 1)
        candidate = result.anchor_candidates[0]
        self.assertEqual(candidate.fact.object_name, "init_var_from_num")
        self.assertEqual(candidate.resolution_tier, 3)
        self.assertFalse(candidate.exact_name)

    def test_relation_search_unique_exact_name_anchor_ignores_text_fallback_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            lock_acquire = _fact("fact:function:lwlock_acquire", "function", "LWLockAcquire")
            lock_acquire_or_wait = _fact(
                "fact:function:lwlock_acquire_or_wait",
                "function",
                "LWLockAcquireOrWait",
            )
            caller = _fact("fact:function:buffer_alloc", "function", "BufferAlloc")
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot(
                [lock_acquire, lock_acquire_or_wait, caller],
                [_relative("rel:call:lwlock_acquire", caller.object_id, lock_acquire.object_id, "direct_call")],
            )

            result = store.relation_search("callers:LWLockAcquire", limit=20)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.total, 1)
        self.assertEqual(result.anchor.object_id, lock_acquire.object_id)
        self.assertEqual(result.anchor_candidates, ())
        self.assertEqual([match.fact.object_id for match in result.matches], [caller.object_id])

    def test_relation_search_depth_two_callees_returns_shortest_hops_and_skips_root_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "function", "root")
            left = _fact("fact:function:left", "function", "left")
            right = _fact("fact:function:right", "function", "right")
            leaf = _fact("fact:function:leaf", "function", "leaf")
            relatives = [
                _relative("rel:root:left", root.object_id, left.object_id, "direct_call"),
                _relative("rel:root:right", root.object_id, right.object_id, "direct_call"),
                _relative("rel:left:leaf", left.object_id, leaf.object_id, "direct_call"),
                _relative("rel:right:leaf", right.object_id, leaf.object_id, "direct_call"),
                _relative("rel:leaf:root", leaf.object_id, root.object_id, "direct_call"),
            ]
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([root, left, right, leaf], relatives)

            result = store.relation_search("callees:root depth:2", limit=20)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.query_kind, "relation_transitive")
        self.assertTrue(result.complete)
        self.assertFalse(result.budget_exhausted)
        self.assertEqual(result.total, 3)
        self.assertEqual([(match.fact.object_name, match.hop) for match in result.matches], [("left", 1), ("right", 1), ("leaf", 2)])
        leaf_match = result.matches[2]
        self.assertEqual(leaf_match.instances, 2)
        self.assertEqual([relation.relation_kind for relation in leaf_match.matched_relations], ["direct_call"])

    def test_relation_search_reachable_returns_shortest_path_and_depth_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "function", "root")
            middle = _fact("fact:function:middle", "function", "middle")
            leaf = _fact("fact:function:leaf", "function", "leaf")
            target_fact = _fact("fact:function:target", "function", "target")
            relatives = [
                _relative("rel:root:middle", root.object_id, middle.object_id, "direct_call"),
                _relative("rel:middle:leaf", middle.object_id, leaf.object_id, "direct_call"),
                _relative("rel:leaf:target", leaf.object_id, target_fact.object_id, "direct_call"),
            ]
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([root, middle, leaf, target_fact], relatives)

            reachable = store.relation_search("reachable:root->target", limit=20)
            bounded = store.relation_search("reachable:root->target depth:2", limit=20)

        self.assertEqual(reachable.status, "ok")
        self.assertEqual(reachable.query_kind, "relation_reachable")
        self.assertTrue(reachable.reachable)
        self.assertTrue(reachable.complete)
        self.assertFalse(reachable.budget_exhausted)
        self.assertEqual([node.fact.object_name for node in reachable.path], ["root", "middle", "leaf", "target"])
        self.assertEqual([node.hop for node in reachable.path], [0, 1, 2, 3])
        self.assertEqual(bounded.status, "ok")
        self.assertFalse(bounded.reachable)
        self.assertFalse(bounded.complete)
        self.assertFalse(bounded.budget_exhausted)
        self.assertEqual(bounded.path, ())
        self.assertEqual(bounded.depth_requested, 2)

    def test_relation_search_reachable_path_nodes_preserve_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "function", "root")
            unguarded = _fact("fact:function:unguarded", "function", "unguarded")
            branched = _fact("fact:function:branched", "function", "branched")
            looped = _fact("fact:function:looped", "function", "looped")
            target_fact = _fact("fact:function:target", "function", "target")
            branch_condition = RelativeCondition(kind="branch", expression="reset_flag", branch="then", source="src/root.c:4")
            loop_condition = RelativeCondition(kind="loop_guard", expression="i < n", branch="body", source="src/branched.c:8")
            compile_condition = RelativeCondition(kind="compile_guard", expression="FEATURE_X", branch="then", source="src/looped.c:1")
            relatives = [
                _relative("rel:root:unguarded", root.object_id, unguarded.object_id, "direct_call"),
                _relative(
                    "rel:unguarded:branched",
                    unguarded.object_id,
                    branched.object_id,
                    "direct_call",
                    condition=branch_condition,
                ),
                _relative(
                    "rel:branched:looped",
                    branched.object_id,
                    looped.object_id,
                    "direct_call",
                    condition=loop_condition,
                ),
                _relative(
                    "rel:looped:target",
                    looped.object_id,
                    target_fact.object_id,
                    "direct_call",
                    condition=compile_condition,
                ),
            ]
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([root, unguarded, branched, looped, target_fact], relatives)

            result = store.relation_search("reachable:root->target", limit=20)

        self.assertTrue(result.reachable)
        self.assertEqual([node.fact.object_name for node in result.path], ["root", "unguarded", "branched", "looped", "target"])
        self.assertEqual(
            [node.condition for node in result.path],
            [None, None, branch_condition, loop_condition, compile_condition],
        )
        self.assertEqual(
            [node.representative_relative_id for node in result.path],
            [None, "rel:root:unguarded", "rel:unguarded:branched", "rel:branched:looped", "rel:looped:target"],
        )

    def test_relation_search_traverses_dispatch_edges_via_assigned_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "function", "root")
            target_fact = _fact("fact:function:target", "function", "target")
            leaf = _fact("fact:function:leaf", "function", "leaf")
            slot = _fact("fact:field:ExecProcNode", "field", "ExecProcNode")
            relatives = [
                _relative("rel:root:slot", root.object_id, slot.object_id, "dispatches_via"),
                _relative("rel:slot:target", slot.object_id, target_fact.object_id, "assigned_to"),
                _relative("rel:target:leaf", target_fact.object_id, leaf.object_id, "direct_call"),
            ]
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([root, target_fact, leaf, slot], relatives)

            callees = store.relation_search("callees:root depth:2", limit=20)
            reachable = store.relation_search("reachable:root->leaf", limit=20)
            dispatch_targets = store.relation_search("dispatches_via:ExecProcNode", limit=20)

        self.assertEqual(callees.status, "ok")
        self.assertEqual(
            [(match.fact.object_name, match.hop, match.matched_relations[0].relation_kind) for match in callees.matches],
            [("target", 1, "dispatches_via"), ("leaf", 2, "direct_call")],
        )
        self.assertEqual(reachable.status, "ok")
        self.assertTrue(reachable.reachable)
        self.assertEqual([node.fact.object_name for node in reachable.path], ["root", "target", "leaf"])
        self.assertEqual([node.relation_kind for node in reachable.path], [None, "dispatches_via", "direct_call"])
        self.assertEqual(dispatch_targets.status, "ok")
        self.assertEqual([(match.fact.object_name, match.matched_relations[0].relation_kind) for match in dispatch_targets.matches], [("target", "assigned_to")])

    def test_relation_search_invalid_depth_needs_refinement_without_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "function", "root")
            leaf = _fact("fact:function:leaf", "function", "leaf")
            slot = _fact("fact:field:slot", "field", "slot")
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot(
                [root, leaf, slot],
                [
                    _relative("rel:root:leaf", root.object_id, leaf.object_id, "direct_call"),
                    _relative("rel:read:slot", root.object_id, slot.object_id, "field_read"),
                ],
            )

            cases = [
                ("callees:root depth:0", "relation_transitive", "callees:root depth:2"),
                ("callees:root depth:-1", "relation_transitive", "callees:root depth:2"),
                ("callees:root depth:2 depth:3", "relation_transitive", "callees:root depth:2"),
                ("readers:slot depth:2", "relation", "readers:slot"),
            ]
            results = [(query, store.relation_search(query, limit=20)) for query, _, _ in cases]

        for query, result in results:
            with self.subTest(query=query):
                expected_kind = next(kind for case_query, kind, _ in cases if case_query == query)
                expected_example = next(example for case_query, _, example in cases if case_query == query)
                self.assertEqual(result.status, "needs_refinement")
                self.assertEqual(result.query_kind, expected_kind)
                self.assertFalse(result.complete)
                self.assertIn("depth", result.message)
                self.assertIn(expected_example, result.examples)

    def test_relation_search_hard_error_messages_include_next_actions(self):
        cases = [
            (
                "readers:fact:field:slot writers:fact:field:slot",
                ["has 2 (readers, writers)", "Keep one and rerun"],
            ),
            (
                "readers:",
                ["relation predicate anchor must be non-empty", "search('<symbol or field name>')", "result.object_id"],
            ),
            (
                "reachable:root",
                ["reachable:<from>-><to>", "search('<function name>')", "reachable:fact:function:start->fact:function:target"],
            ),
            (
                "readers:fact:field:slot condition:branch",
                ["condition filter is not supported", "detail(<fact_id>)", "`condition` field"],
            ),
        ]

        for query, fragments in cases:
            with self.subTest(query=query):
                with self.assertRaises(StorageError) as caught:
                    storage_module.parse_relation_search_query(query)
                self.assertEqual(caught.exception.code, "invalid_relation_query")
                for fragment in fragments:
                    self.assertIn(fragment, caught.exception.message)

    def test_relation_search_one_hop_too_broad_is_complete_with_exact_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            field = _fact("fact:field:slot", "field", "slot")
            readers = [_fact(f"fact:function:reader:{index:02d}", "function", f"reader_{index:02d}") for index in range(5)]
            relatives = [
                _relative(f"rel:reader:{index:02d}", reader.object_id, field.object_id, "field_read")
                for index, reader in enumerate(readers)
            ]
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([field, *readers], relatives)

            result = store.relation_search("readers:slot", limit=2)

        self.assertEqual(result.status, "too_broad")
        self.assertTrue(result.complete)
        self.assertFalse(result.budget_exhausted)
        self.assertEqual(result.total, 5)
        self.assertEqual(len(result.matches), 2)

    def test_relation_search_empty_writers_guides_to_accessors(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            field = _fact("fact:field:slot", "field", "slot")
            reader = _fact("fact:function:reader", "function", "reader")
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot(
                [field, reader],
                [_relative("rel:read:slot", reader.object_id, field.object_id, "field_read")],
            )

            writers = store.relation_search(f"writers:{field.object_id}", limit=20)
            accessors = store.relation_search(f"accessors:{field.object_id}", limit=20)

        self.assertEqual(writers.status, "ok")
        self.assertEqual(writers.total, 0)
        self.assertEqual(writers.matches, ())
        self.assertIn(f"accessors:{field.object_id}", writers.message)
        self.assertEqual(writers.examples, (f"accessors:{field.object_id}",))
        self.assertEqual([match.fact.object_id for match in accessors.matches], [reader.object_id])

    def test_relation_search_frontier_budget_reports_incomplete_partial_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            root = _fact("fact:function:root", "function", "root")
            a = _fact("fact:function:a", "function", "a")
            b = _fact("fact:function:b", "function", "b")
            c = _fact("fact:function:c", "function", "c")
            relatives = [
                _relative("rel:root:a", root.object_id, a.object_id, "direct_call"),
                _relative("rel:root:b", root.object_id, b.object_id, "direct_call"),
                _relative("rel:b:c", b.object_id, c.object_id, "direct_call"),
            ]
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([root, a, b, c], relatives)

            with patch.object(storage_module, "RELATION_TRANSITIVE_FRONTIER_BUDGET", 1):
                result = store.relation_search("callees:root depth:2", limit=20)

        self.assertEqual(result.status, "too_broad")
        self.assertEqual(result.query_kind, "relation_transitive")
        self.assertFalse(result.complete)
        self.assertTrue(result.budget_exhausted)
        self.assertEqual(result.budget_exhausted_kind, "frontier_edges")
        self.assertGreaterEqual(result.frontier_edge_count, 1)

    def test_replace_snapshot_rejects_duplicate_relative_and_missing_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)

            with self.assertRaises(StorageError) as duplicate:
                store.replace_snapshot(
                    _facts(),
                    [
                        _relative("rel:dup", "fact:file:a", "fact:file:b", "include"),
                        _relative("rel:dup", "fact:file:a", "fact:file:b", "include"),
                    ],
                )
            self.assertEqual(duplicate.exception.code, "duplicate_relative_id")

            with self.assertRaises(StorageError) as missing:
                store.replace_snapshot(_facts(), [_relative("rel:missing", "fact:missing", "fact:file:b", "include")])
            self.assertEqual(missing.exception.code, "relative_endpoint_missing")

    def test_relations_reject_invalid_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            store.replace_snapshot(_facts(), [_relative("rel:include", "fact:file:a", "fact:file:b", "include")])

            cases = [
                ({"fact_id": "", "direction": "both", "limit": 20}, "invalid_fact_id"),
                ({"fact_id": "fact:file:a", "direction": "sideways", "limit": 20}, "invalid_direction"),
                ({"fact_id": "fact:file:a", "direction": "both", "relation_kind": "bad", "limit": 20}, "invalid_relation_kind"),
                ({"fact_id": "fact:file:a", "direction": "both", "limit": 0}, "invalid_limit"),
                ({"fact_id": "fact:file:a", "direction": "both", "limit": 101}, "invalid_limit"),
            ]

            for kwargs, code in cases:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(StorageError) as caught:
                        store.relatives_for_fact(**kwargs)
                    self.assertEqual(caught.exception.code, code)

            count_cases = [
                ({"fact_id": "", "direction": "both"}, "invalid_fact_id"),
                ({"fact_id": "fact:file:a", "direction": "sideways"}, "invalid_direction"),
                ({"fact_id": "fact:file:a", "direction": "both", "relation_kind": "bad"}, "invalid_relation_kind"),
            ]
            for kwargs, code in count_cases:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(StorageError) as caught:
                        store.count_relatives_for_fact(**kwargs)
                    self.assertEqual(caught.exception.code, code)

    def test_relation_kind_error_lists_supported_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            store.replace_snapshot(_facts(), [_relative("rel:include", "fact:file:a", "fact:file:b", "include")])

            with self.assertRaises(StorageError) as listed:
                store.relatives_for_fact("fact:file:a", direction="both", relation_kind="bad", limit=20)
            with self.assertRaises(StorageError) as counted:
                store.count_relatives_for_fact("fact:file:a", direction="both", relation_kind="bad")

        for caught in (listed, counted):
            with self.subTest(message=caught.exception.message):
                self.assertEqual(caught.exception.code, "invalid_relation_kind")
                self.assertIn("Supported kinds:", caught.exception.message)
                self.assertIn(storage_module.RELATION_KIND_GUIDANCE_LIST, caught.exception.message)


if __name__ == "__main__":
    unittest.main()

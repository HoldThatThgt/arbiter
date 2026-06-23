# Migrated from cipher-2 tests/test_code_extractor_fixtures.py (M4 facts absorption acceptance).
# Rewrites per docs/proposals/m4-test-migration-map.md:
#   * cipher2.initializer.extractor.code -> arbiter_engine.facts.extractor.code.
#   * cipher2.config.{load_config,write_default_config} -> c2.initializer_support shims (no config
#     file; build/resolve a 6-field ExtractorConfig and stash-then-return it). map §1.2/§3.
#   * cipher2.tools.log.open_log -> the extractor's real jsonl log (arbiter_engine.facts.extractor.code).
#   * cipher2.storage -> arbiter_engine.facts.store.
# The cipher2_* AST node identifiers (probe record/callee, cipher2_condition, cipher2HeaderCacheHit)
# stay byte-aligned with the absorbed mapper's wire vocabulary (map §1.3).
import json
import shutil
import tempfile
import unittest
from dataclasses import fields
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Optional
from unittest import mock

from arbiter_engine.facts.extractor import code as code_extractor
from arbiter_engine.facts.extractor.code import CodeFact, CodeFactExtractor, DirectCallEvidence, open_log
from arbiter_engine.facts.store import FactRecord, FactRelative, RelativeCondition, StoredFactLine, StoredRelativeLine, open_fact_store
from c2.initializer_support import load_config, write_default_config
from c2.toolchain_helpers import write_fake_toolchain


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_compile_database(path: Path, entries) -> None:
    _write(path, json.dumps(entries, sort_keys=True))


def _loc(line: int, file: Optional[str] = None, col: Optional[int] = None):
    value = {"line": line}
    if file is not None:
        value["file"] = file
    if col is not None:
        value["col"] = col
    return value


def _qtype(text: str):
    return {"qualType": text}


def _test_relative(relative_id: str, *, payload_rank: int = 1) -> FactRelative:
    return FactRelative(
        relative_id=relative_id,
        from_fact_id="code:function:caller",
        to_fact_id="code:function:callee",
        relation_kind="direct_call",
        condition=None,
        object_profile="debug",
        evidence_source=f"src/main.c:{payload_rank}",
        confidence=1.0,
        payload={"rank": payload_rank},
    )


def _write_relative_segment(segment_dir: Path, relatives) -> code_extractor._MapSegmentManifest:
    item = code_extractor._FileWorkItem(
        seq=0,
        source=segment_dir / "source.c",
        rel_source="src/source.c",
        profile="debug",
        source_id="source:test",
        compile_lookup=code_extractor._CompileCommandLookup(
            configured=False,
            matched=False,
            entry=None,
            flags=[],
            command_hash=None,
            argument_count=0,
            stripped_argument_count=0,
        ),
        segment_dir=segment_dir,
    )
    return code_extractor._write_file_map_segments(
        item,
        code_extractor._FileMapResult(
            facts=[],
            relatives=list(relatives),
            unresolved_calls=[],
            stats=code_extractor._FileMapStats(),
        ),
    )


def _write_relative_segment_with_deduper(
    segment_dir: Path,
    relatives,
    *,
    deduper: Optional[code_extractor._WorkerRelativeDeduper] = None,
) -> code_extractor._MapSegmentManifest:
    item = code_extractor._FileWorkItem(
        seq=0,
        source=segment_dir / "source.c",
        rel_source="src/source.c",
        profile="debug",
        source_id="source:test",
        compile_lookup=code_extractor._CompileCommandLookup(
            configured=False,
            matched=False,
            entry=None,
            flags=[],
            command_hash=None,
            argument_count=0,
            stripped_argument_count=0,
        ),
        segment_dir=segment_dir,
    )
    return code_extractor._write_file_map_segments(
        item,
        code_extractor._FileMapResult(
            facts=[],
            relatives=list(relatives),
            unresolved_calls=[],
            stats=code_extractor._FileMapStats(),
        ),
        relative_deduper=deduper,
    )


def _field_id(owner: str, name: str) -> str:
    return f"field:{owner}:{name}"


def _field_decl(owner: str, name: str, line: int = 2, file: Optional[str] = None, type_text: str = "int"):
    return {
        "id": _field_id(owner, name),
        "kind": "FieldDecl",
        "name": name,
        "loc": _loc(line, file),
        "type": _qtype(type_text),
    }


def _member_expr(
    owner: str = "Context",
    name: str = "member",
    line: int = 6,
    file: Optional[str] = None,
    type_text: str = "int",
):
    return {
        "kind": "MemberExpr",
        "name": name,
        "loc": _loc(line, file),
        "type": _qtype(type_text),
        "referencedMemberDecl": _field_id(owner, name),
    }


def _function_decl_ref(name: str, line: int, referenced_file: Optional[str] = None):
    return {
        "kind": "DeclRefExpr",
        "name": name,
        "loc": _loc(line),
        "type": _qtype("int (void)"),
        "referencedDecl": {
            "kind": "FunctionDecl",
            "name": name,
            "loc": _loc(line, referenced_file),
            "type": _qtype("int (void)"),
        },
    }


def _var_decl_ref(name: str, line: int, type_text: str = "int (*)(void)", *, is_local: bool = True):
    referenced = {
        "kind": "VarDecl",
        "name": name,
        "loc": _loc(line),
        "type": _qtype(type_text),
    }
    if is_local:
        referenced["isLocal"] = True
        referenced["storageClass"] = "auto"
    return {
        "kind": "DeclRefExpr",
        "name": name,
        "loc": _loc(line),
        "type": _qtype(type_text),
        "referencedDecl": referenced,
    }


def _header_global_decl(header_file: str, name: str = "get_attavgwidth_hook"):
    return {
        "kind": "VarDecl",
        "name": name,
        "loc": _loc(1, header_file),
        "type": _qtype("int (*)(void)"),
        "storageClass": "extern",
    }


def _call_expr(
    name: str,
    line: int,
    *args,
    referenced_file: Optional[str] = None,
    condition: Optional[dict] = None,
):
    node = {
        "kind": "CallExpr",
        "loc": _loc(line),
        "type": _qtype("int"),
        "inner": [
            {
                "kind": "DeclRefExpr",
                "name": name,
                "loc": _loc(line),
                "type": _qtype("int (void)"),
                "referencedDecl": {
                    "kind": "FunctionDecl",
                    "name": name,
                    "loc": _loc(line, referenced_file),
                    "type": _qtype("int (void)"),
                },
            },
            *args,
        ],
    }
    if condition is not None:
        node["cipher2_condition"] = condition
    return node


class CodeExtractorFixturesTest(unittest.TestCase):
    def test_repo_relative_source_cache_reuses_path_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source_location_base = target / "src"
            file_value = "../include/shared.h"
            expected = code_extractor._repo_relative_source_from_file_value(
                target,
                source_location_base,
                file_value,
            )

            resolve_calls = []
            original_resolve = code_extractor.Path.resolve

            def counting_resolve(path, *args, **kwargs):
                resolve_calls.append(str(path))
                return original_resolve(path, *args, **kwargs)

            with mock.patch.object(code_extractor.Path, "resolve", counting_resolve):
                cache = code_extractor._RepoRelativeSourceCache()
                for _ in range(5):
                    self.assertEqual(
                        code_extractor._repo_relative_source_from_file_value(
                            target,
                            source_location_base,
                            file_value,
                            cache=cache,
                        ),
                        expected,
                    )

        self.assertEqual(expected, "include/shared.h")
        self.assertEqual(cache.entry_count(), 1)
        self.assertEqual(len(resolve_calls), 2)

    def test_direct_call_evidence_json_round_trips_all_fields(self):
        evidence = DirectCallEvidence(
            caller_fact_id="code:function:caller",
            callee_name="target",
            referenced_source="src/target.c",
            evidence_source="src/caller.c:7",
            condition=RelativeCondition(kind="branch", expression="enabled", branch="then", source="src/caller.c:6"),
        )

        row = evidence.to_json()
        restored = DirectCallEvidence.from_json(row)

        self.assertEqual(set(row), {field.name for field in fields(DirectCallEvidence)})
        self.assertEqual(restored, evidence)
        self.assertEqual(restored.condition.expression, "enabled")

    def test_code_fact_to_fact_record_preserves_contract_fields(self):
        fact = CodeFact(
            fact_kind="function",
            object_id="code:function:abc",
            object_name="helper",
            object_description="function helper",
            object_source="src/main.c:4",
            object_profile="debug",
            object_caller=None,
            object_callee=None,
            payload={"source_kind": "c", "line": 4},
        )

        record = fact.to_fact_record()

        self.assertIsInstance(record, FactRecord)
        self.assertEqual(record.object_id, "code:function:abc")
        self.assertIsNone(record.object_caller)
        self.assertIsNone(record.object_callee)
        self.assertEqual(record.payload["fact_kind"], "function")
        self.assertEqual(record.payload["source_kind"], "c")

    def test_code_fact_is_frozen_slotted(self):
        fact = CodeFact(
            fact_kind="function",
            object_id="code:function:abc",
            object_name="helper",
            object_description="function helper",
            object_source="src/main.c:4",
            object_profile="debug",
            payload={"source_kind": "c", "line": 4},
        )

        self.assertFalse(hasattr(fact, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            fact.object_name = "changed"

    def test_global_identity_ignores_ordinal_source_id_and_materializing_translation_unit(self):
        first = code_extractor._object_identity_payload(
            fact_kind="global",
            name="get_attavgwidth_hook",
            line_number=67,
            caller=None,
            callee=None,
            profile="debug",
            payload={
                "canonical_source": "src/include/utils/lsyscache.h",
                "linkage": "extern",
                "source_id": "source:a",
                "ordinal": 3237,
            },
        )
        second = code_extractor._object_identity_payload(
            fact_kind="global",
            name="get_attavgwidth_hook",
            line_number=67,
            caller=None,
            callee=None,
            profile="debug",
            payload={
                "canonical_source": "src/include/utils/lsyscache.h",
                "linkage": "extern",
                "source_id": "source:b",
                "ordinal": 2826,
            },
        )

        self.assertEqual(first, second)
        self.assertEqual(first["canonical_source"], "src/include/utils/lsyscache.h")
        self.assertEqual(first["linkage"], "extern")
        self.assertNotIn("ordinal", first)
        self.assertNotIn("source_id", first)

    def test_macro_identity_is_location_keyed_and_ignores_ordinal(self):
        # The same header macro reached by two translation units must dedup to a
        # single fact at the reducer, so its object identity must depend only on
        # canonical_source + line + name, never on the per-mapper traversal
        # ordinal (which differs by how many facts precede it in each TU).
        first = code_extractor._object_identity_payload(
            fact_kind="macro",
            name="DATA_LIMIT",
            line_number=12,
            caller=None,
            callee=None,
            profile="debug",
            payload={"canonical_source": "include/limits.h", "ordinal": 7},
        )
        second = code_extractor._object_identity_payload(
            fact_kind="macro",
            name="DATA_LIMIT",
            line_number=12,
            caller=None,
            callee=None,
            profile="debug",
            payload={"canonical_source": "include/limits.h", "ordinal": 4221},
        )

        self.assertEqual(first, second)
        self.assertEqual(first["canonical_source"], "include/limits.h")
        self.assertEqual(first["line"], 12)
        self.assertNotIn("ordinal", first)

    def test_deeply_nested_expression_skips_one_tu_without_aborting_snapshot(self):
        # A translation unit whose AST nests far deeper than Python's recursion
        # limit (the shape clang emits for a long `a + b + c ...` chain found in
        # generated DBMS parsers) must be skipped and recorded, not abort the
        # whole snapshot. A healthy sibling TU in the same run must still index.
        def _deep_binary_chain(depth: int, line: int) -> dict:
            node = {"kind": "DeclRefExpr", "name": "a", "loc": _loc(line)}
            for _ in range(depth):
                node = {
                    "kind": "BinaryOperator",
                    "opcode": "+",
                    "loc": _loc(line),
                    "inner": [node, {"kind": "IntegerLiteral", "loc": _loc(line)}],
                }
            return node

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            deep_source = target / "src" / "deep.c"
            healthy_source = target / "src" / "healthy.c"
            _write(deep_source, "int boom(void) { return 0; }\n")
            _write(healthy_source, "int fine(void) { return 1; }\n")

            deep_ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "FunctionDecl",
                        "name": "boom",
                        "loc": _loc(1, "src/deep.c"),
                        "isThisDeclarationADefinition": True,
                        "inner": [
                            {
                                "kind": "CompoundStmt",
                                "inner": [
                                    {
                                        "kind": "ReturnStmt",
                                        "loc": _loc(2),
                                        "inner": [_deep_binary_chain(1500, 2)],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            healthy_ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "FunctionDecl",
                        "name": "fine",
                        "loc": _loc(1, "src/healthy.c"),
                        "isThisDeclarationADefinition": True,
                        "inner": [{"kind": "CompoundStmt", "inner": [{"kind": "ReturnStmt", "loc": _loc(1)}]}],
                    }
                ],
            }
            ast_by_rel = {"src/deep.c": deep_ast, "src/healthy.c": healthy_ast}

            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel)
            # The whole point: collect() returns instead of propagating RecursionError.
            result = extractor.collect([deep_source, healthy_source], "debug")

            function_names = {fact.object_name for fact in result.facts if fact.fact_kind == "function"}
            self.assertIn("fine", function_names)
            self.assertNotIn("boom", function_names)
            recorded = {(getattr(error, "code", None), getattr(error, "source", None)) for error in result.errors}
            self.assertIn(("map_failed", "src/deep.c"), recorded)

    def test_iterative_ast_walkers_handle_depth_beyond_recursion_limit(self):
        # Direct guard on the hot walkers themselves: at a depth that overflows
        # an equivalent recursive walk they must traverse without raising.
        depth = 1500
        node = {"kind": "DeclRefExpr", "name": "a"}
        for index in range(depth):
            node = {
                "kind": "BinaryOperator",
                "opcode": "+",
                "inner": [node, {"kind": "DeclRefExpr", "name": f"v{index}"}],
            }
        deepest = node
        for _ in range(depth):
            deepest = code_extractor._node_children(deepest)[0]

        visited = sum(1 for _ in code_extractor._walk_dicts(node))
        self.assertEqual(visited, depth * 2 + 1)
        self.assertTrue(code_extractor._contains_dict(node, deepest))

    def test_oversized_relative_condition_is_budgeted_below_storage_cap(self):
        # _compact_condition_text caps only the expression text; a long
        # expression plus a long "path:line" source still overflows the store's
        # 1 KB RelativeCondition ceiling and raises a non-recoverable
        # StorageError. The mapper budgets the condition holistically so it can
        # be constructed, truncating (or dropping) the expression as needed.
        long_source = "src/backend/" + "nested/dir/" * 40 + "file.c:98765"
        compacted = code_extractor._compact_condition_text("value_field + " * 64)
        self.assertEqual(len(compacted), code_extractor.CONDITION_TEXT_MAX_CHARS)

        naive_bytes = len(
            json.dumps(
                {"kind": "branch", "expression": compacted, "branch": "then", "source": long_source},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        self.assertGreater(naive_bytes, code_extractor.CONDITION_MAX_BYTES)

        budgeted = code_extractor._budget_condition_expression("branch", compacted, "then", long_source)
        self.assertIsNotNone(budgeted)
        self.assertLess(len(budgeted), len(compacted))
        # Must construct without raising StorageError and stay within the cap.
        condition = RelativeCondition(kind="branch", expression=budgeted, branch="then", source=long_source)
        encoded = json.dumps(condition.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(encoded), code_extractor.CONDITION_MAX_BYTES)

    def test_oversized_condition_falls_back_to_no_expression_when_source_fills_budget(self):
        # When the fixed fields (notably a very long source) already consume the
        # budget, the expression is dropped entirely rather than overflowing.
        huge_source = "x/" * 600 + "f.c:1"
        budgeted = code_extractor._budget_condition_expression(
            "branch", code_extractor._compact_condition_text("a + b + c"), "then", huge_source
        )
        self.assertIsNone(budgeted)

    def test_condition_for_node_drops_condition_when_source_alone_exceeds_cap(self):
        # Dropping the expression is not enough when `source` alone overflows the
        # cap: building the RelativeCondition would still raise a non-recoverable
        # StorageError. _condition_for_node is the single choke point into
        # RelativeCondition and must fall back to an unconditional relative
        # (return None) rather than emit an oversized condition.
        huge_source = "x/" * 600 + "f.c:1"
        self.assertGreater(len(huge_source.encode("utf-8")), code_extractor.CONDITION_MAX_BYTES)
        node = {
            "kind": "CallExpr",
            "cipher2_condition": {
                "kind": "branch",
                "expression": None,
                "branch": "then",
                "source": huge_source,
            },
        }
        # Must NOT raise StorageError and must drop the over-cap condition.
        self.assertIsNone(code_extractor._condition_for_node(node))

        # A normal, within-cap condition still round-trips into a RelativeCondition.
        ok_node = {
            "kind": "CallExpr",
            "cipher2_condition": {
                "kind": "branch",
                "expression": "enabled",
                "branch": "then",
                "source": "src/main.c:12",
            },
        }
        condition = code_extractor._condition_for_node(ok_node)
        self.assertIsNotNone(condition)
        self.assertEqual(condition.expression, "enabled")

    def test_capture_lines_handles_null_inner_child_without_dropping_siblings(self):
        # clang's JSON AST can place a literal null in an `inner` array for an
        # omitted optional sub-node. The iterative _capture_lines must traverse
        # past it and still record every following sibling's line/file context,
        # exactly as the previous recursive form did.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            mapper = code_extractor._ClangAstMapper(target, "src/main.c", "c", "debug", "source:main")
            trailing = {"kind": "VarDecl", "name": "z", "loc": _loc(3, "src/main.c")}
            root = {
                "kind": "TranslationUnitDecl",
                "loc": _loc(1, "src/main.c"),
                "inner": [
                    {"kind": "VarDecl", "name": "a", "loc": _loc(2, "src/main.c")},
                    None,
                    trailing,
                ],
            }
            mapper._capture_lines(root, 1, None)
            # The sibling after the null child must still be recorded.
            self.assertEqual(mapper._line_by_node.get(id(trailing)), 3)
            self.assertEqual(mapper._file_by_node.get(id(trailing)), "src/main.c")

    def test_streaming_reducer_spools_snapshot_shaped_fact_lines(self):
        fact = CodeFact(
            fact_kind="function",
            object_id="code:function:abc",
            object_name="helper",
            object_description="function helper",
            object_source="src/main.c:4",
            object_profile="debug",
            payload={"source_kind": "c", "line": 4},
        )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            extractor = CodeFactExtractor(target, load_config(target, observe=False))
            with extractor.stream([], "debug") as extraction:
                self.assertTrue(extraction._spool_fact(fact, 0))
                encoded = list(extraction._iter_spooled_encoded_facts())

        self.assertEqual(len(encoded), 1)
        expected = StoredFactLine.from_fact(fact.to_fact_record()).to_json()
        self.assertEqual(json.loads(encoded[0].line_text), expected)
        self.assertIn("payload_sha256", expected)

    def test_direct_call_resolver_rebuilds_index_from_spooled_scalars(self):
        helper = CodeFact(
            fact_kind="function",
            object_id="code:function:helper",
            object_name="helper",
            object_description="function helper",
            object_source="src/helper.c:1",
            object_profile="debug",
            payload={"name": "helper", "canonical_source": "src/helper.c", "linkage": "external"},
        )
        entry = CodeFact(
            fact_kind="function",
            object_id="code:function:entry",
            object_name="entry",
            object_description="function entry",
            object_source="src/entry.c:1",
            object_profile="debug",
            payload={"name": "entry", "canonical_source": "src/entry.c", "linkage": "external"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            extractor = CodeFactExtractor(target, load_config(target, observe=False))
            with extractor.stream([], "debug") as extraction:
                self.assertTrue(extraction._spool_fact(helper, 0))
                self.assertTrue(extraction._spool_fact(entry, 0))
                extraction._spool_unresolved_call(
                    DirectCallEvidence("code:function:entry", "helper", "src/helper.c", "src/entry.c:2")
                )

                def fail_materialized_fact_rebuild():
                    raise AssertionError("direct_call resolver must not rebuild full CodeFact objects")

                extraction._iter_spooled_facts = fail_materialized_fact_rebuild  # type: ignore[method-assign]
                extraction._resolve_and_spool_direct_calls()
                index_values = list(extraction._direct_call_index.functions_by_id.values())
                encoded_relatives = list(extraction._iter_spooled_encoded_relatives())

        self.assertTrue(index_values)
        self.assertFalse(any(isinstance(value, CodeFact) for value in index_values))
        self.assertEqual(len(encoded_relatives), 1)
        relative = code_extractor._relative_from_encoded_relative_line(encoded_relatives[0])
        self.assertEqual(relative.from_fact_id, entry.object_id)
        self.assertEqual(relative.to_fact_id, helper.object_id)
        self.assertEqual(relative.payload["resolution_strategy"], "exact_source")

    def test_external_relative_merge_uses_sorted_sidecar_without_full_payload_parse(self):
        earlier = _test_relative("rel:001", payload_rank=1)
        later = _test_relative("rel:002", payload_rank=2)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            manifest = _write_relative_segment(target / "seg", [later, earlier])

            with mock.patch.object(
                code_extractor.StoredRelativeLine,
                "from_json",
                side_effect=AssertionError("external relative merge must not full-parse segment payload"),
            ):
                stats = code_extractor._RelativeExternalMergeStats()
                encoded = list(code_extractor._iter_external_merged_relative_segments([manifest], stats))

        self.assertEqual([relative.relative_id for relative in encoded], ["rel:001", "rel:002"])
        self.assertEqual(stats.input_count, 2)
        self.assertEqual(stats.accepted_count, 2)
        self.assertEqual(stats.duplicate_exact_count, 0)
        self.assertEqual(stats.full_parse_count, 0)
        self.assertEqual(json.loads(encoded[0].read_line_text()), StoredRelativeLine.from_relative(earlier).to_json())

    def test_external_relative_merge_within_fan_in_uses_single_pass(self):
        relatives = [_test_relative(f"rel:{index:03d}", payload_rank=index) for index in range(3)]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            manifests = [_write_relative_segment(target / f"seg-{index}", [relative]) for index, relative in enumerate(relatives)]
            stats = code_extractor._RelativeExternalMergeStats()
            encoded = list(code_extractor._iter_external_merged_relative_segments(manifests, stats, fan_in=8))

        self.assertEqual([relative.relative_id for relative in encoded], ["rel:000", "rel:001", "rel:002"])
        self.assertEqual(stats.input_count, 3)
        self.assertEqual(stats.accepted_count, 3)
        self.assertEqual(stats.fan_in, 3)
        self.assertEqual(stats.pass_count, 1)
        self.assertEqual(stats.max_heap_size, 3)
        self.assertEqual(stats.peak_open_segment_count, 3)

    def test_external_relative_merge_over_fan_in_uses_multipass_bounded_segments(self):
        relatives = [_test_relative(f"rel:{index:03d}", payload_rank=index) for index in range(11)]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            manifests = [_write_relative_segment(target / f"seg-{index}", [relative]) for index, relative in enumerate(relatives)]
            single_pass_stats = code_extractor._RelativeExternalMergeStats()
            single_pass = list(
                code_extractor._iter_external_merged_relative_segments(manifests, single_pass_stats, fan_in=64)
            )
            multipass_stats = code_extractor._RelativeExternalMergeStats()
            multipass = list(
                code_extractor._iter_external_merged_relative_segments(manifests, multipass_stats, fan_in=4)
            )

        self.assertEqual([relative.read_line_text() for relative in multipass], [relative.read_line_text() for relative in single_pass])
        self.assertEqual(multipass_stats.input_count, 11)
        self.assertEqual(multipass_stats.accepted_count, 11)
        self.assertEqual(multipass_stats.fan_in, 4)
        self.assertGreater(multipass_stats.pass_count, 1)
        self.assertLessEqual(multipass_stats.max_heap_size, 4)
        self.assertLessEqual(multipass_stats.peak_open_segment_count, 4)
        self.assertEqual(multipass_stats.full_parse_count, 0)

    def test_external_relative_merge_fails_closed_on_non_exact_duplicate_id(self):
        first = _test_relative("rel:001", payload_rank=1)
        conflicting = _test_relative("rel:001", payload_rank=2)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            left = _write_relative_segment(target / "left", [first])
            right = _write_relative_segment(target / "right", [conflicting])

            with self.assertRaises(Exception) as caught:
                list(code_extractor._iter_external_merged_relative_segments([left, right], code_extractor._RelativeExternalMergeStats()))

        self.assertEqual(getattr(caught.exception, "code", None), "map_reduce_conflict")
        self.assertEqual(caught.exception.details["relative_id"], "rel:001")

    def test_worker_relative_dedup_skips_exact_duplicate_before_segment_write(self):
        first = _test_relative("rel:001", payload_rank=1)
        duplicate = _test_relative("rel:001", payload_rank=1)
        later = _test_relative("rel:002", payload_rank=2)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            deduper = code_extractor._WorkerRelativeDeduper()
            manifest = _write_relative_segment_with_deduper(target / "seg", [later, first, duplicate], deduper=deduper)
            relative_lines = (target / "seg" / "relatives.jsonl").read_text(encoding="utf-8").splitlines()
            index_lines = (target / "seg" / "relatives.index").read_text(encoding="utf-8").splitlines()
            stats = deduper.snapshot()

        self.assertEqual(manifest.relative_count, 2)
        self.assertEqual(manifest.relative_map_input_count, 3)
        self.assertEqual(manifest.relative_map_written_count, 2)
        self.assertEqual(manifest.relative_map_skipped_exact_count, 1)
        self.assertEqual(manifest.relative_worker_duplicate_exact_count, 1)
        self.assertEqual(manifest.relative_worker_duplicate_conflict_count, 0)
        self.assertEqual(manifest.relative_worker_dedup_tracked_entry_count, 2)
        self.assertEqual(manifest.relative_worker_dedup_saturated_count, 0)
        self.assertEqual(len(relative_lines), 2)
        self.assertEqual(len(index_lines), 2)
        self.assertEqual(stats.relative_map_input_count, 3)
        self.assertEqual(stats.relative_map_written_count, 2)
        self.assertEqual(stats.relative_map_skipped_exact_count, 1)

    def test_worker_relative_dedup_fails_closed_on_non_exact_duplicate_id(self):
        first = _test_relative("rel:001", payload_rank=1)
        conflicting = _test_relative("rel:001", payload_rank=2)

        with tempfile.TemporaryDirectory() as tmp:
            deduper = code_extractor._WorkerRelativeDeduper()
            with self.assertRaises(Exception) as caught:
                _write_relative_segment_with_deduper(Path(tmp) / "seg", [first, conflicting], deduper=deduper)

        self.assertEqual(getattr(caught.exception, "code", None), "map_reduce_conflict")
        self.assertEqual(caught.exception.details["relative_id"], "rel:001")
        self.assertEqual(deduper.snapshot().relative_worker_duplicate_conflict_count, 1)

    def test_worker_relative_dedup_saturation_keeps_untracked_relatives(self):
        first = _test_relative("rel:001", payload_rank=1)
        duplicate = _test_relative("rel:001", payload_rank=1)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            deduper = code_extractor._WorkerRelativeDeduper(max_tracked_bytes=0)
            manifest = _write_relative_segment_with_deduper(target / "seg", [first, duplicate], deduper=deduper)
            relative_lines = (target / "seg" / "relatives.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(manifest.relative_count, 2)
        self.assertEqual(manifest.relative_map_input_count, 2)
        self.assertEqual(manifest.relative_map_written_count, 2)
        self.assertEqual(manifest.relative_map_skipped_exact_count, 0)
        self.assertEqual(manifest.relative_worker_duplicate_exact_count, 0)
        self.assertEqual(manifest.relative_worker_dedup_tracked_entry_count, 0)
        self.assertGreater(manifest.relative_worker_dedup_saturated_count, 0)
        self.assertEqual(len(relative_lines), 2)

    def test_map_reduce_duplicate_fact_prefers_min_source_seq_then_strict_superset(self):
        partial_fact = CodeFact(
            fact_kind="function",
            object_id="code:function:abc",
            object_name="helper",
            object_description="function helper",
            object_source="include/helper.h:4",
            object_profile="debug",
            payload={"name": "helper"},
        )
        complete_fact = CodeFact(
            fact_kind="function",
            object_id="code:function:abc",
            object_name="helper",
            object_description="function helper",
            object_source="include/helper.h:4",
            object_profile="debug",
            payload={"name": "helper", "canonical_source": "include/helper.h", "line": 4},
        )
        later_tu_fact = CodeFact(
            fact_kind="function",
            object_id="code:function:abc",
            object_name="helper",
            object_description="function helper",
            object_source="include/helper.h:4",
            object_profile="debug",
            payload={
                "name": "helper",
                "canonical_source": "include/helper.h",
                "line": 4,
                "source_id": "source:zzz",
                "ordinal": 96,
                "linkage": "unknown",
            },
        )
        earlier_tu_fact = CodeFact(
            fact_kind="function",
            object_id="code:function:abc",
            object_name="helper",
            object_description="function helper",
            object_source="include/helper.h:4",
            object_profile="debug",
            payload={
                "name": "helper",
                "canonical_source": "include/helper.h",
                "line": 4,
                "source_id": "source:aaa",
                "ordinal": 6,
                "linkage": "external",
            },
        )
        conflict = CodeFact(
            fact_kind="function",
            object_id="code:function:abc",
            object_name="helper",
            object_description="function helper",
            object_source="include/helper.h:4",
            object_profile="debug",
            payload={"name": "helper", "canonical_source": "include/other.h"},
        ).to_json()

        partial = partial_fact.to_json()
        complete = complete_fact.to_json()
        self.assertEqual(code_extractor._merge_duplicate_fact_json(partial, complete), complete)
        self.assertEqual(code_extractor._merge_duplicate_fact_json(complete, partial), complete)
        self.assertIsNone(code_extractor._merge_duplicate_fact_json(complete, conflict))

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            extractor = CodeFactExtractor(target, load_config(target, observe=False))
            with extractor.stream([], "debug") as extraction:
                self.assertTrue(extraction._spool_fact(later_tu_fact, 1))
                self.assertFalse(extraction._spool_fact(earlier_tu_fact, 0))
                facts = list(extraction._iter_spooled_facts())

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].payload["source_id"], "source:aaa")
        self.assertEqual(facts[0].payload["ordinal"], 6)
        self.assertEqual(facts[0].payload["linkage"], "external")

    def test_extractor_emits_expected_fact_kinds_from_c_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(
                target / "driver.c",
                """
#define DRIVER_HOOK 1
typedef int (*handler_t)(int);
enum Mode { MODE_A };
int global_counter = 0;
int helper(int value) { return value + 1; }
int entry(int value) {
  handler_t handler = helper;
  global_counter = helper(value);
  return handler(value);
}
""".strip()
                + "\n",
            )
            write_fake_toolchain(target)

            extractor = CodeFactExtractor(target, load_config(target, observe=False))
            result = extractor.collect([target / "driver.c"], "debug")
            facts = result.facts

            kinds = {fact.fact_kind for fact in facts}
            self.assertTrue({"code_file", "function", "global", "type", "macro", "function_pointer_slot"} <= kinds)
            relation_kinds = {relative.relation_kind for relative in result.relatives}
            self.assertTrue({"defines", "direct_call", "assigned_to", "dispatches_via"} <= relation_kinds)
            function_names = {fact.object_name for fact in facts if fact.fact_kind == "function"}
            self.assertEqual(function_names, {"helper", "entry"})
            helper = next(fact for fact in facts if fact.fact_kind == "function" and fact.object_name == "helper")
            entry = next(fact for fact in facts if fact.fact_kind == "function" and fact.object_name == "entry")
            direct_call = next(relative for relative in result.relatives if relative.relation_kind == "direct_call")
            self.assertEqual(direct_call.from_fact_id, entry.object_id)
            self.assertEqual(direct_call.to_fact_id, helper.object_id)
            self.assertNotIn(str(target), str([fact.payload for fact in facts]))

    def test_header_and_multiple_source_files_are_scanned_deterministically(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "include" / "driver.h", "#define LIMIT 8\nstruct Header { int id; };\n")
            _write(target / "src" / "a.c", '#include "driver.h"\nint alpha(void) { return 1; }\n')
            _write(target / "src" / "b.cpp", "int beta(void) { return alpha(); }\n")
            write_fake_toolchain(target)

            extractor = CodeFactExtractor(target, load_config(target, observe=False))
            first = list(extractor.extract([target / "include", target / "src"], "default"))
            second_result = extractor.collect([target / "include", target / "src"], "default")
            second = second_result.facts

            self.assertEqual([fact.object_id for fact in first], [fact.object_id for fact in second])
            self.assertTrue(any(fact.object_source.startswith("include/driver.h:") for fact in first))
            self.assertTrue(any(fact.object_source.startswith("src/a.c:") for fact in first))
            self.assertTrue(any(fact.object_source.startswith("src/b.cpp:") for fact in first))
            self.assertIn("include", {relative.relation_kind for relative in second_result.relatives})

    def test_clang_member_expr_emits_field_read_and_write_relatives(self):
        ast = {
            "kind": "TranslationUnitDecl",
            "inner": [
                {
                    "kind": "RecordDecl",
                    "name": "Context",
                    "loc": {"line": 1},
                    "completeDefinition": True,
                    "inner": [_field_decl("Context", "member")],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "write_member",
                    "loc": {"line": 5},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "BinaryOperator",
                                    "opcode": "=",
                                    "loc": {"line": 6},
                                    "inner": [
                                        {
                                            **_member_expr(line=6),
                                        },
                                        {"kind": "IntegerLiteral", "loc": {"line": 6}},
                                    ],
                                }
                            ],
                        }
                    ],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "read_member",
                    "loc": {"line": 9},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    **_call_expr("consume", 10, _member_expr(line=10)),
                                }
                            ],
                        }
                    ],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "bump_member",
                    "loc": {"line": 13},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "CompoundAssignOperator",
                                    "opcode": "+=",
                                    "loc": {"line": 14},
                                    "inner": [
                                        {
                                            **_member_expr(line=14),
                                        },
                                        {"kind": "IntegerLiteral", "loc": {"line": 14}},
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "context.c", "struct Context { int member; };\n")
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)

            result = extractor.collect([target / "src" / "context.c"], "debug")

        fields = {fact.object_name: fact for fact in result.facts if fact.fact_kind == "field"}
        self.assertIn("member", fields)
        self.assertEqual(fields["member"].payload["owner_name"], "Context")
        functions = {fact.object_name: fact for fact in result.facts if fact.fact_kind == "function"}
        by_kind = {}
        for relative in result.relatives:
            by_kind.setdefault(relative.relation_kind, []).append(relative)

        reads = by_kind["field_read"]
        writes = by_kind["field_write"]
        self.assertEqual({relative.to_fact_id for relative in reads + writes}, {fields["member"].object_id})
        self.assertIn(functions["read_member"].object_id, {relative.from_fact_id for relative in reads})
        self.assertIn(functions["bump_member"].object_id, {relative.from_fact_id for relative in reads})
        self.assertIn(functions["write_member"].object_id, {relative.from_fact_id for relative in writes})
        self.assertIn(functions["bump_member"].object_id, {relative.from_fact_id for relative in writes})
        self.assertIn("read_write", {relative.payload.get("access_context") for relative in reads + writes})

    def test_unstable_field_access_operator_context_is_partial_read(self):
        ast = {
            "kind": "TranslationUnitDecl",
            "inner": [
                {
                    "kind": "RecordDecl",
                    "name": "Context",
                    "loc": {"line": 1},
                    "completeDefinition": True,
                    "inner": [_field_decl("Context", "member")],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "ambiguous_member",
                    "loc": {"line": 5},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "BinaryOperator",
                                    "loc": {"line": 6},
                                    "inner": [
                                        _member_expr(line=6),
                                        {"kind": "IntegerLiteral", "loc": {"line": 6}},
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "context.c", "struct Context { int member; };\n")
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)

            result = extractor.collect([target / "src" / "context.c"], "debug")

        reads = [relative for relative in result.relatives if relative.relation_kind == "field_read"]
        writes = [relative for relative in result.relatives if relative.relation_kind == "field_write"]
        self.assertEqual(len(reads), 1)
        self.assertEqual(writes, [])
        self.assertEqual(reads[0].payload["access_context"], "rvalue_partial")
        self.assertEqual(reads[0].payload["access_confidence"], "partial")

    def test_wrapped_macro_bitwise_member_exprs_keep_field_access_semantics(self):
        ast = {
            "kind": "TranslationUnitDecl",
            "inner": [
                {
                    "kind": "RecordDecl",
                    "name": "Context",
                    "loc": {"line": 1},
                    "completeDefinition": True,
                    "inner": [_field_decl("Context", "flags")],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "check_flags",
                    "loc": {"line": 5},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "IfStmt",
                                    "loc": {"line": 6},
                                    "inner": [
                                        {
                                            "kind": "BinaryOperator",
                                            "opcode": "&",
                                            "loc": {"line": 6},
                                            "inner": [
                                                {
                                                    "kind": "CStyleCastExpr",
                                                    "loc": {"line": 6},
                                                    "inner": [
                                                        {
                                                            "kind": "ParenExpr",
                                                            "loc": {"line": 6},
                                                            "inner": [
                                                                {
                                                                    **_member_expr("Context", "flags", 6),
                                                                    "loc": {
                                                                        "line": 6,
                                                                        "spellingLoc": {"line": 2},
                                                                        "expansionLoc": {"line": 6},
                                                                    },
                                                                }
                                                            ],
                                                        }
                                                    ],
                                                },
                                                {"kind": "IntegerLiteral", "loc": {"line": 6}},
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "assign_flags",
                    "loc": {"line": 10},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "BinaryOperator",
                                    "opcode": "=",
                                    "loc": {"line": 11},
                                    "inner": [
                                        {
                                            "kind": "ParenExpr",
                                            "loc": {"line": 11},
                                            "inner": [_member_expr("Context", "flags", 11)],
                                        },
                                        {"kind": "IntegerLiteral", "loc": {"line": 11}},
                                    ],
                                }
                            ],
                        }
                    ],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "or_flags",
                    "loc": {"line": 15},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "CompoundAssignOperator",
                                    "opcode": "|=",
                                    "loc": {"line": 16},
                                    "inner": [
                                        {
                                            "kind": "ParenExpr",
                                            "loc": {"line": 16},
                                            "inner": [_member_expr("Context", "flags", 16)],
                                        },
                                        {"kind": "IntegerLiteral", "loc": {"line": 16}},
                                    ],
                                }
                            ],
                        }
                    ],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "binary_or_assign_flags",
                    "loc": {"line": 20},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "BinaryOperator",
                                    "opcode": "|=",
                                    "loc": {"line": 21},
                                    "inner": [
                                        {
                                            "kind": "ParenExpr",
                                            "loc": {"line": 21},
                                            "inner": [_member_expr("Context", "flags", 21)],
                                        },
                                        {"kind": "IntegerLiteral", "loc": {"line": 21}},
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "context.c", "struct Context { int flags; };\n")
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast, log_enabled=True)

            result = extractor.collect([target / "src" / "context.c"], "debug")
            file_event = next(
                event
                for event in open_log(target).read_events(channel="initializer").events
                if event.event_name == "extractor.code.file"
            )

        fields = {fact.object_name: fact for fact in result.facts if fact.fact_kind == "field"}
        functions = {fact.object_name: fact for fact in result.facts if fact.fact_kind == "function"}
        reads = [relative for relative in result.relatives if relative.relation_kind == "field_read"]
        writes = [relative for relative in result.relatives if relative.relation_kind == "field_write"]
        self.assertEqual({relative.to_fact_id for relative in reads + writes}, {fields["flags"].object_id})
        self.assertIn(functions["check_flags"].object_id, {relative.from_fact_id for relative in reads})
        self.assertIn(functions["assign_flags"].object_id, {relative.from_fact_id for relative in writes})
        self.assertNotIn(functions["assign_flags"].object_id, {relative.from_fact_id for relative in reads})
        self.assertIn(functions["or_flags"].object_id, {relative.from_fact_id for relative in reads})
        self.assertIn(functions["or_flags"].object_id, {relative.from_fact_id for relative in writes})
        self.assertIn(functions["binary_or_assign_flags"].object_id, {relative.from_fact_id for relative in reads})
        self.assertIn(functions["binary_or_assign_flags"].object_id, {relative.from_fact_id for relative in writes})
        self.assertIn("condition", {relative.payload.get("access_context") for relative in reads})
        self.assertIn("assignment_lhs", {relative.payload.get("access_context") for relative in writes})
        self.assertIn("read_write", {relative.payload.get("access_context") for relative in reads + writes})
        self.assertEqual(file_event.counts["field_access_resolved_count"], 4)
        self.assertEqual(file_event.counts["field_access_unresolved_count"], 0)
        self.assertEqual(file_event.counts["wrapped_member_expr_count"], 4)
        self.assertEqual(file_event.counts["macro_wrapped_member_expr_count"], 1)
        self.assertEqual(file_event.counts["bitwise_member_expr_count"], 2)
        self.assertEqual(file_event.counts["compound_field_access_count"], 2)
        self.assertEqual(file_event.counts["field_access_scan_truncated_count"], 0)
        self.assertEqual(file_event.counts["unresolved_dispatch_slot_count"], 0)
        self.assertEqual(file_event.counts["unresolved_dispatch_function_count"], 0)

    def test_function_pointer_dispatch_uses_field_global_and_local_slots(self):
        fp_type = "int (*)(void)"
        ast = {
            "kind": "TranslationUnitDecl",
            "inner": [
                {
                    "kind": "RecordDecl",
                    "name": "Ops",
                    "loc": {"line": 1},
                    "completeDefinition": True,
                    "inner": [_field_decl("Ops", "read", line=2, type_text=fp_type)],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "my_read",
                    "loc": {"line": 4},
                    "isThisDeclarationADefinition": True,
                    "inner": [{"kind": "CompoundStmt", "inner": []}],
                },
                {
                    "kind": "VarDecl",
                    "name": "global_cb",
                    "loc": {"line": 7},
                    "type": _qtype(fp_type),
                    "inner": [_function_decl_ref("my_read", 7)],
                },
                {
                    "kind": "VarDecl",
                    "name": "ops_instance",
                    "loc": {"line": 8},
                    "type": _qtype("struct Ops"),
                    "inner": [
                        {
                            "kind": "InitListExpr",
                            "loc": {"line": 8},
                            "inner": [
                                {
                                    "kind": "DesignatedInitExpr",
                                    "loc": {"line": 8},
                                    "referencedDecl": {
                                        "id": _field_id("Ops", "read"),
                                        "kind": "FieldDecl",
                                        "name": "read",
                                        "loc": _loc(2),
                                        "type": _qtype(fp_type),
                                    },
                                    "inner": [_function_decl_ref("my_read", 8)],
                                }
                            ],
                        }
                    ],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "driver",
                    "loc": {"line": 10},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "VarDecl",
                                    "name": "local_cb",
                                    "loc": {"line": 11},
                                    "type": _qtype(fp_type),
                                    "isLocal": True,
                                    "storageClass": "auto",
                                    "inner": [_function_decl_ref("my_read", 11)],
                                },
                                {
                                    "kind": "BinaryOperator",
                                    "opcode": "=",
                                    "loc": {"line": 12},
                                    "inner": [
                                        _member_expr("Ops", "read", 12, type_text=fp_type),
                                        _function_decl_ref("my_read", 12),
                                    ],
                                },
                                {
                                    "kind": "CallExpr",
                                    "loc": {"line": 13},
                                    "type": _qtype("int"),
                                    "inner": [_member_expr("Ops", "read", 13, type_text=fp_type)],
                                },
                                {
                                    "kind": "CallExpr",
                                    "loc": {"line": 14},
                                    "type": _qtype("int"),
                                    "inner": [_var_decl_ref("local_cb", 11, fp_type)],
                                },
                                {
                                    "kind": "CallExpr",
                                    "loc": {"line": 15},
                                    "type": _qtype("int"),
                                    "inner": [_var_decl_ref("global_cb", 7, fp_type, is_local=False)],
                                },
                                {
                                    "kind": "CallExpr",
                                    "loc": {"line": 16, "spellingLoc": {"line": 3}, "expansionLoc": {"line": 16}},
                                    "type": _qtype("int"),
                                    "inner": [_function_decl_ref("my_read", 16)],
                                },
                            ],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "dispatch.c", "struct Ops { int (*read)(void); };\n")
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast, log_enabled=True)

            result = extractor.collect([target / "src" / "dispatch.c"], "debug")
            file_event = next(
                event
                for event in open_log(target).read_events(channel="initializer").events
                if event.event_name == "extractor.code.file"
            )

        facts_by_kind_name = {(fact.fact_kind, fact.object_name): fact for fact in result.facts}
        read_field = facts_by_kind_name[("field", "read")]
        global_slot = facts_by_kind_name[("global", "global_cb")]
        local_slots = [fact for fact in result.facts if fact.fact_kind == "function_pointer_slot" and fact.object_name == "local_cb"]
        self.assertEqual(len(local_slots), 1)
        local_slot = local_slots[0]
        target_function = facts_by_kind_name[("function", "my_read")]
        driver_function = facts_by_kind_name[("function", "driver")]

        assigned = [relative for relative in result.relatives if relative.relation_kind == "assigned_to"]
        dispatches = [relative for relative in result.relatives if relative.relation_kind == "dispatches_via"]
        direct_calls = [relative for relative in result.relatives if relative.relation_kind == "direct_call"]

        self.assertTrue(any(relative.from_fact_id == read_field.object_id and relative.to_fact_id == target_function.object_id for relative in assigned))
        self.assertTrue(any(relative.from_fact_id == global_slot.object_id and relative.to_fact_id == target_function.object_id for relative in assigned))
        self.assertTrue(any(relative.from_fact_id == local_slot.object_id and relative.to_fact_id == target_function.object_id for relative in assigned))
        self.assertEqual(
            {relative.to_fact_id for relative in dispatches if relative.from_fact_id == driver_function.object_id},
            {read_field.object_id, global_slot.object_id, local_slot.object_id},
        )
        self.assertTrue(any(relative.from_fact_id == driver_function.object_id and relative.to_fact_id == target_function.object_id for relative in direct_calls))
        self.assertEqual(file_event.counts["function_pointer_slot_count"], 1)
        self.assertEqual(file_event.counts["function_pointer_assignment_count"], 4)
        self.assertEqual(file_event.counts["function_pointer_dispatch_count"], 3)
        self.assertEqual(file_event.counts["macro_direct_call_count"], 1)
        self.assertEqual(file_event.counts["unresolved_dispatch_slot_count"], 0)
        self.assertEqual(file_event.counts["unresolved_dispatch_function_count"], 0)

    def test_function_pointer_dispatch_uses_desugared_member_typedef_type(self):
        alias_type = {
            "qualType": "ExecProcNodeMtd",
            "desugaredQualType": "int (*)(void *)",
            "typeAliasDeclId": "typedef:ExecProcNodeMtd",
        }
        ast = {
            "kind": "TranslationUnitDecl",
            "inner": [
                {
                    "kind": "TypedefDecl",
                    "name": "ExecProcNodeMtd",
                    "loc": {"line": 1},
                    "type": _qtype("int (*)(void *)"),
                },
                {
                    "kind": "RecordDecl",
                    "name": "PlanState",
                    "loc": {"line": 2},
                    "completeDefinition": True,
                    "inner": [
                        {
                            "id": _field_id("PlanState", "ExecProcNode"),
                            "kind": "FieldDecl",
                            "name": "ExecProcNode",
                            "loc": _loc(3),
                            "type": alias_type,
                        }
                    ],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "ExecLeaf",
                    "loc": {"line": 5},
                    "isThisDeclarationADefinition": True,
                    "inner": [{"kind": "CompoundStmt", "inner": []}],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "ExecDriver",
                    "loc": {"line": 8},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "CallExpr",
                                    "loc": {"line": 9},
                                    "type": _qtype("int"),
                                    "inner": [
                                        {
                                            "kind": "ImplicitCastExpr",
                                            "loc": {"line": 9},
                                            "type": alias_type,
                                            "inner": [
                                                _member_expr(
                                                    "PlanState",
                                                    "ExecProcNode",
                                                    9,
                                                    type_text="ExecProcNodeMtd",
                                                )
                                                | {"type": alias_type}
                                            ],
                                        },
                                        {"kind": "DeclRefExpr", "name": "node", "loc": _loc(9), "type": _qtype("struct PlanState *")},
                                    ],
                                }
                            ],
                        }
                    ],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "ExecInit",
                    "loc": {"line": 12},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "BinaryOperator",
                                    "opcode": "=",
                                    "loc": {"line": 13},
                                    "type": alias_type,
                                    "inner": [
                                        _member_expr(
                                            "PlanState",
                                            "ExecProcNode",
                                            13,
                                            type_text="ExecProcNodeMtd",
                                        )
                                        | {"type": alias_type},
                                        _function_decl_ref("ExecLeaf", 13),
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "exec.c", "typedef int (*ExecProcNodeMtd)(void *);\n")
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast, log_enabled=True)

            result = extractor.collect([target / "src" / "exec.c"], "debug")
            file_event = next(
                event
                for event in open_log(target).read_events(channel="initializer").events
                if event.event_name == "extractor.code.file"
            )

        facts_by_kind_name = {(fact.fact_kind, fact.object_name): fact for fact in result.facts}
        exec_proc_node = facts_by_kind_name[("field", "ExecProcNode")]
        exec_leaf = facts_by_kind_name[("function", "ExecLeaf")]
        exec_driver = facts_by_kind_name[("function", "ExecDriver")]
        assigned = [relative for relative in result.relatives if relative.relation_kind == "assigned_to"]
        dispatches = [relative for relative in result.relatives if relative.relation_kind == "dispatches_via"]

        self.assertTrue(
            any(relative.from_fact_id == exec_proc_node.object_id and relative.to_fact_id == exec_leaf.object_id for relative in assigned)
        )
        self.assertTrue(
            any(relative.from_fact_id == exec_driver.object_id and relative.to_fact_id == exec_proc_node.object_id for relative in dispatches)
        )
        self.assertEqual(file_event.counts["function_pointer_assignment_count"], 1)
        self.assertEqual(file_event.counts["function_pointer_dispatch_count"], 1)
        self.assertEqual(file_event.counts["unresolved_dispatch_slot_count"], 0)
        self.assertEqual(file_event.counts["unresolved_dispatch_function_count"], 0)

    def test_function_pointer_dispatch_does_not_guess_non_pointer_member_call(self):
        ast = {
            "kind": "TranslationUnitDecl",
            "inner": [
                {
                    "kind": "RecordDecl",
                    "name": "Ops",
                    "loc": {"line": 1},
                    "completeDefinition": True,
                    "inner": [_field_decl("Ops", "value", line=2, type_text="int")],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "driver",
                    "loc": {"line": 5},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "CallExpr",
                                    "loc": {"line": 6},
                                    "type": _qtype("int"),
                                    "inner": [_member_expr("Ops", "value", 6, type_text="int")],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "dispatch.c", "struct Ops { int value; };\n")
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast, log_enabled=True)

            result = extractor.collect([target / "src" / "dispatch.c"], "debug")
            file_event = next(
                event
                for event in open_log(target).read_events(channel="initializer").events
                if event.event_name == "extractor.code.file"
            )

        self.assertNotIn("dispatches_via", {relative.relation_kind for relative in result.relatives})
        self.assertEqual(file_event.counts["function_pointer_dispatch_count"], 0)
        self.assertEqual(file_event.counts["unresolved_dispatch_slot_count"], 1)

    def test_clang_implicit_line_numbers_inherit_previous_explicit_line_for_relatives(self):
        ast = {
            "kind": "TranslationUnitDecl",
            "inner": [
                {
                    "kind": "RecordDecl",
                    "name": "Context",
                    "loc": {"line": 1},
                    "completeDefinition": True,
                    "inner": [_field_decl("Context", "member")],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "read_twice",
                    "loc": {"line": 10},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {"kind": "DeclStmt", "loc": {"line": 20}},
                                {
                                    **_call_expr(
                                        "consume",
                                        20,
                                        {
                                            **_member_expr(line=20),
                                            "loc": {},
                                            "range": {"begin": {"offset": 128, "col": 13}},
                                        },
                                    ),
                                    "loc": {},
                                    "range": {"begin": {"offset": 120, "col": 5}},
                                },
                                {"kind": "DeclStmt", "loc": {"line": 21}},
                                {
                                    **_call_expr(
                                        "consume",
                                        21,
                                        {
                                            **_member_expr(line=21),
                                            "loc": {},
                                            "range": {"begin": {"offset": 158, "col": 13}},
                                        },
                                    ),
                                    "loc": {},
                                    "range": {"begin": {"offset": 150, "col": 5}},
                                },
                            ],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "context.c", "struct Context { int member; };\n")
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)

            result = extractor.collect([target / "src" / "context.c"], "debug")

        reads = [relative for relative in result.relatives if relative.relation_kind == "field_read"]
        self.assertEqual(len(reads), 2)
        self.assertEqual({relative.evidence_source for relative in reads}, {"src/context.c:20", "src/context.c:21"})
        self.assertEqual({relative.payload["line"] for relative in reads}, {20, 21})
        self.assertEqual(len({relative.relative_id for relative in reads}), 2)

    def test_clang_same_line_duplicate_relative_ids_are_deduped_in_mapper(self):
        ast = {
            "kind": "TranslationUnitDecl",
            "inner": [
                {
                    "kind": "RecordDecl",
                    "name": "Context",
                    "loc": {"line": 1},
                    "completeDefinition": True,
                    "inner": [_field_decl("Context", "member")],
                },
                {
                    "kind": "FunctionDecl",
                    "name": "read_same_line",
                    "loc": {"line": 10},
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                _call_expr("consume", 20, _member_expr(line=20)),
                                _call_expr("consume", 20, _member_expr(line=20)),
                            ],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "context.c", "struct Context { int member; };\n")
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)

            result = extractor.collect([target / "src" / "context.c"], "debug")

        reads = [relative for relative in result.relatives if relative.relation_kind == "field_read"]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].evidence_source, "src/context.c:20")
        self.assertEqual(len({relative.relative_id for relative in result.relatives}), len(result.relatives))

    def test_anonymous_union_fields_materialize_and_resolve_member_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "outer.c"
            _write(source, "struct Outer { union { int a; int b; }; };\n")
            source_file = source.as_posix()
            ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "RecordDecl",
                        "name": "Outer",
                        "tagUsed": "struct",
                        "loc": _loc(1, source_file, 8),
                        "completeDefinition": True,
                        "inner": [
                            {
                                "kind": "RecordDecl",
                                "tagUsed": "union",
                                "loc": _loc(2, source_file, 3),
                                "completeDefinition": True,
                                "inner": [
                                    {"id": "field:anon:a", "kind": "FieldDecl", "name": "a", "loc": _loc(2, source_file, 15), "type": _qtype("int")},
                                    {"id": "field:anon:b", "kind": "FieldDecl", "name": "b", "loc": _loc(2, source_file, 22), "type": _qtype("int")},
                                ],
                            },
                            {"id": "field:carrier", "kind": "FieldDecl", "loc": _loc(2, source_file, 3), "type": _qtype("union Outer::(anonymous)")},
                            {"id": "field:indirect:a", "kind": "IndirectFieldDecl", "name": "a", "loc": _loc(2, source_file, 15), "type": _qtype("int")},
                            {"id": "field:indirect:b", "kind": "IndirectFieldDecl", "name": "b", "loc": _loc(2, source_file, 22), "type": _qtype("int")},
                        ],
                    },
                    {
                        "kind": "FunctionDecl",
                        "name": "read_union",
                        "loc": _loc(5, source_file, 1),
                        "type": _qtype("int (struct Outer *)"),
                        "isThisDeclarationADefinition": True,
                        "inner": [
                            {
                                "kind": "CompoundStmt",
                                "inner": [
                                    _call_expr(
                                        "consume",
                                        6,
                                        {
                                            "kind": "MemberExpr",
                                            "name": "a",
                                            "loc": _loc(6, source_file, 20),
                                            "type": _qtype("int"),
                                            "referencedMemberDecl": "field:anon:a",
                                            "inner": [
                                                {
                                                    "kind": "MemberExpr",
                                                    "name": "",
                                                    "loc": _loc(6, source_file, 20),
                                                    "type": _qtype("union Outer::(anonymous)"),
                                                    "referencedMemberDecl": "field:carrier",
                                                }
                                            ],
                                        },
                                    ),
                                    _call_expr(
                                        "consume",
                                        7,
                                        {
                                            "kind": "MemberExpr",
                                            "name": "b",
                                            "loc": _loc(7, source_file, 20),
                                            "type": _qtype("int"),
                                            "referencedMemberDecl": "field:indirect:b",
                                        },
                                    ),
                                ],
                            }
                        ],
                    },
                ],
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)
            extractor.log_enabled = True

            result = extractor.collect([source], "debug")
            file_event = next(event for event in open_log(target).read_events(channel="initializer").events if event.event_name == "extractor.code.file")

        owner_name = "Outer::<anonymous-union>@src/outer.c:2:3"
        field_facts = [fact for fact in result.facts if fact.fact_kind == "field"]
        self.assertEqual(len(field_facts), 2)
        fields = {fact.object_name: fact for fact in field_facts}
        self.assertEqual({"a", "b"}, set(fields))
        self.assertEqual(fields["a"].payload["owner_name"], owner_name)
        self.assertEqual(fields["b"].payload["owner_name"], owner_name)
        owner_type = next(fact for fact in result.facts if fact.fact_kind == "type" and fact.object_name == owner_name)
        has_field_targets = {relative.to_fact_id for relative in result.relatives if relative.relation_kind == "has_field" and relative.from_fact_id == owner_type.object_id}
        self.assertEqual(has_field_targets, {fields["a"].object_id, fields["b"].object_id})
        reads = [relative for relative in result.relatives if relative.relation_kind == "field_read"]
        self.assertEqual({relative.to_fact_id for relative in reads}, {fields["a"].object_id, fields["b"].object_id})
        self.assertEqual(file_event.counts["field_decl_count"], 4)
        self.assertEqual(file_event.counts["field_fact_count"], 4)
        self.assertEqual(file_event.counts["field_decl_without_fact_count"], 0)
        self.assertEqual(file_event.counts["field_access_resolved_count"], 2)
        self.assertEqual(file_event.counts["field_access_unresolved_count"], 0)

    def test_indirect_field_without_canonical_target_reports_field_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "outer.c"
            _write(source, "struct Outer { union { int a; }; };\n")
            source_file = source.as_posix()
            ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "RecordDecl",
                        "name": "Outer",
                        "tagUsed": "struct",
                        "loc": _loc(1, source_file, 8),
                        "completeDefinition": True,
                        "inner": [
                            {"id": "field:indirect:a", "kind": "IndirectFieldDecl", "name": "a", "loc": _loc(2, source_file, 15), "type": _qtype("int")},
                        ],
                    }
                ],
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)
            extractor.log_enabled = True

            result = extractor.collect([source], "debug")
            file_event = next(event for event in open_log(target).read_events(channel="initializer").events if event.event_name == "extractor.code.file")

        self.assertEqual([fact for fact in result.facts if fact.fact_kind == "field"], [])
        self.assertEqual(file_event.counts["field_decl_count"], 1)
        self.assertEqual(file_event.counts["field_fact_count"], 0)
        self.assertEqual(file_event.counts["field_decl_without_fact_count"], 1)

    def test_same_line_anonymous_records_have_distinct_synthetic_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "outer.c"
            _write(source, "struct Outer { struct { int left; }; struct { int right; }; };\n")
            source_file = source.as_posix()
            ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "RecordDecl",
                        "name": "Outer",
                        "tagUsed": "struct",
                        "loc": _loc(1, source_file, 8),
                        "completeDefinition": True,
                        "inner": [
                            {
                                "kind": "RecordDecl",
                                "tagUsed": "struct",
                                "loc": _loc(2, source_file, 3),
                                "completeDefinition": True,
                                "inner": [{"id": "field:anon:left", "kind": "FieldDecl", "name": "left", "loc": _loc(2, source_file, 16), "type": _qtype("int")}],
                            },
                            {"id": "field:carrier:left", "kind": "FieldDecl", "loc": _loc(2, source_file, 3), "type": _qtype("struct Outer::(anonymous left)")},
                            {
                                "kind": "RecordDecl",
                                "tagUsed": "struct",
                                "loc": _loc(2, source_file, 30),
                                "completeDefinition": True,
                                "inner": [{"id": "field:anon:right", "kind": "FieldDecl", "name": "right", "loc": _loc(2, source_file, 44), "type": _qtype("int")}],
                            },
                            {"id": "field:carrier:right", "kind": "FieldDecl", "loc": _loc(2, source_file, 30), "type": _qtype("struct Outer::(anonymous right)")},
                        ],
                    }
                ],
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)

            result = extractor.collect([source], "debug")

        fields = {fact.object_name: fact for fact in result.facts if fact.fact_kind == "field"}
        self.assertEqual({"left", "right"}, set(fields))
        owners = {fields["left"].payload["owner_name"], fields["right"].payload["owner_name"]}
        self.assertEqual(len(owners), 2)
        self.assertIn("Outer::<anonymous-struct>@src/outer.c:2:3", owners)
        self.assertIn("Outer::<anonymous-struct>@src/outer.c:2:30", owners)

    def test_missing_type_fact_still_materializes_named_record_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "widget.cpp"
            _write(source, "struct Widget { int flag; };\n")
            source_file = source.as_posix()
            ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "CXXRecordDecl",
                        "name": "Widget",
                        "tagUsed": "struct",
                        "loc": _loc(1, source_file, 8),
                        "completeDefinition": True,
                        "inner": [{"id": "field:Widget:flag", "kind": "FieldDecl", "name": "flag", "loc": _loc(1, source_file, 21), "type": _qtype("int")}],
                    },
                    {
                        "kind": "FunctionDecl",
                        "name": "read_widget",
                        "loc": _loc(2, source_file, 1),
                        "isThisDeclarationADefinition": True,
                        "inner": [
                            {
                                "kind": "CompoundStmt",
                                "inner": [_call_expr("consume", 2, _member_expr("Widget", "flag", line=2, file=source_file))],
                            }
                        ],
                    },
                ],
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)

            result = extractor.collect([source], "debug")

        widget_type = next(fact for fact in result.facts if fact.fact_kind == "type" and fact.object_name == "Widget")
        flag = next(fact for fact in result.facts if fact.fact_kind == "field" and fact.object_name == "flag")
        self.assertEqual(flag.payload["owner_type_id"], widget_type.object_id)
        self.assertIn(flag.object_id, {relative.to_fact_id for relative in result.relatives if relative.relation_kind == "has_field"})
        self.assertIn(flag.object_id, {relative.to_fact_id for relative in result.relatives if relative.relation_kind == "field_read"})

    def test_header_inline_uses_loc_file_identity_across_translation_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "inline.h"
            _write(header, "static inline int helper(void) { return 1; }\n")
            _write(target / "src" / "a.c", '#include "../include/inline.h"\nint a(void) { return helper(); }\n')
            _write(target / "src" / "b.c", '#include "../include/inline.h"\nint b(void) { return helper(); }\n')
            header_file = header.as_posix()
            ast_by_rel = {
                "src/a.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "helper",
                            "loc": _loc(1, header_file),
                            "type": _qtype("int (void)"),
                            "storageClass": "static",
                            "isThisDeclarationADefinition": True,
                            "inner": [{"kind": "CompoundStmt", "inner": []}],
                        }
                    ],
                },
                "src/b.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "helper",
                            "loc": _loc(1, header_file),
                            "type": _qtype("int (void)"),
                            "storageClass": "static",
                            "isThisDeclarationADefinition": True,
                            "inner": [{"kind": "CompoundStmt", "inner": []}],
                        }
                    ],
                },
            }
            sources = [target / "src" / "a.c", target / "src" / "b.c"]
            serial = _SyntheticAstExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 1}}, observe=False),
                ast_by_rel,
            ).collect(sources, "debug")
            parallel = _SyntheticAstExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 2}}, observe=False),
                ast_by_rel,
            ).collect(sources, "debug")

        self.assertEqual(_extraction_signature(parallel), _extraction_signature(serial))
        result = parallel

        helpers = [fact for fact in result.facts if fact.fact_kind == "function" and fact.object_name == "helper"]
        self.assertEqual(len(helpers), 1)
        self.assertEqual(helpers[0].object_source, "include/inline.h:1")
        self.assertEqual(helpers[0].payload["canonical_source"], "include/inline.h")

    def test_header_global_identity_ignores_materializing_translation_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "hooks.h"
            a = target / "src" / "a.c"
            b = target / "src" / "b.c"
            _write(header, "extern int (*get_attavgwidth_hook)(void);\n")
            _write(a, '#include "../include/hooks.h"\n')
            _write(b, '#include "../include/hooks.h"\n')
            header_file = header.as_posix()
            ast_by_rel = {
                "src/a.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [_header_global_decl(header_file)],
                },
                "src/b.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "MacroDefinitionRecord",
                            "name": "B_BEFORE_HOOK",
                            "loc": _loc(1, b.as_posix()),
                        },
                        _header_global_decl(header_file),
                    ],
                },
            }

            a_result = _SyntheticAstExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 1}}, observe=False),
                {"src/a.c": ast_by_rel["src/a.c"]},
            ).collect([a], "debug")
            b_result = _SyntheticAstExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 1}}, observe=False),
                {"src/b.c": ast_by_rel["src/b.c"]},
            ).collect([b], "debug")

        a_hook = next(fact for fact in a_result.facts if fact.fact_kind == "global")
        b_hook = next(fact for fact in b_result.facts if fact.fact_kind == "global")
        self.assertNotEqual(a_hook.payload["source_id"], b_hook.payload["source_id"])
        self.assertNotEqual(a_hook.payload["ordinal"], b_hook.payload["ordinal"])
        self.assertEqual(a_hook.object_id, b_hook.object_id)
        self.assertEqual(a_hook.payload["canonical_source"], "include/hooks.h")
        self.assertEqual(b_hook.payload["canonical_source"], "include/hooks.h")

    def test_same_named_static_globals_remain_distinct_for_different_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            a = target / "src" / "a.c"
            b = target / "src" / "b.c"
            _write(a, "static int hook;\n")
            _write(b, "static int hook;\n")
            ast_by_rel = {
                "src/a.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "VarDecl",
                            "name": "hook",
                            "loc": _loc(1, a.as_posix()),
                            "type": _qtype("int"),
                            "storageClass": "static",
                        }
                    ],
                },
                "src/b.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "VarDecl",
                            "name": "hook",
                            "loc": _loc(1, b.as_posix()),
                            "type": _qtype("int"),
                            "storageClass": "static",
                        }
                    ],
                },
            }
            result = _SyntheticAstExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 1}}, observe=False),
                ast_by_rel,
            ).collect([a, b], "debug")

        globals_ = [fact for fact in result.facts if fact.fact_kind == "global" and fact.object_name == "hook"]
        self.assertEqual(len(globals_), 2)
        self.assertEqual({fact.payload["canonical_source"] for fact in globals_}, {"src/a.c", "src/b.c"})
        self.assertEqual(len({fact.object_id for fact in globals_}), 2)

    def test_unnamed_top_level_var_decl_is_not_materialized_as_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "anonymous.c"
            _write(source, "int;\n")
            ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "VarDecl",
                        "loc": _loc(1, source.as_posix()),
                        "type": _qtype("int"),
                        "storageClass": "extern",
                    }
                ],
            }
            result = _SyntheticAstExtractor(target, load_config(target, observe=False), ast).collect([source], "debug")

        self.assertFalse(any(fact.fact_kind == "global" for fact in result.facts))

    def test_header_inline_uses_inherited_header_context_when_function_loc_file_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "src" / "include" / "port" / "atomics.h"
            a = target / "src" / "backend" / "utils" / "a.c"
            b = target / "src" / "backend" / "utils" / "b.c"
            _write(
                header,
                "static inline __attribute__((always_inline)) int pg_atomic_compare_exchange_u32(int x) { return x; }\n",
            )
            _write(a, '#include "../../include/port/atomics.h"\nint call_a(int x) { return pg_atomic_compare_exchange_u32(x); }\n')
            _write(b, '#include "../../include/port/atomics.h"\nint call_b(int x) { return pg_atomic_compare_exchange_u32(x); }\n')
            header_file = header.as_posix()

            def included_loc(source_path: Path, line: int, col: int):
                return {
                    "line": line,
                    "col": col,
                    "includedFrom": {"file": source_path.as_posix()},
                }

            def header_file_loc(line: int, col: int):
                return {
                    "line": line,
                    "col": col,
                    "file": header_file,
                }

            def preceding_header_decl():
                return {
                    "kind": "FunctionDecl",
                    "name": "pg_atomic_init_u32",
                    "loc": header_file_loc(341, 1),
                    "type": _qtype("void (int *)"),
                    "storageClass": "static",
                    "inline": True,
                    "isThisDeclarationADefinition": True,
                    "inner": [{"kind": "CompoundStmt", "inner": []}],
                }

            def atomic_decl(source_path: Path):
                return {
                    "kind": "FunctionDecl",
                    "name": "pg_atomic_compare_exchange_u32",
                    "loc": included_loc(source_path, 349, 1),
                    "range": {
                        "begin": included_loc(source_path, 349, 1),
                        "end": included_loc(source_path, 349, 96),
                    },
                    "type": _qtype("int (int)"),
                    "storageClass": "static",
                    "inline": True,
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "ParmVarDecl",
                            "name": "x",
                            "loc": included_loc(source_path, 349, 82),
                            "type": _qtype("int"),
                        },
                        {
                            "kind": "CompoundStmt",
                            "range": {
                                "begin": included_loc(source_path, 349, 85),
                                "end": included_loc(source_path, 349, 96),
                            },
                            "inner": [],
                        },
                        {
                            "kind": "AlwaysInlineAttr",
                            "range": {
                                "begin": included_loc(source_path, 349, 24),
                                "end": included_loc(source_path, 349, 36),
                            },
                        },
                    ],
                }

            def caller_decl(source_path: Path, name: str):
                return {
                    "kind": "FunctionDecl",
                    "name": name,
                    "loc": _loc(2, source_path.as_posix()),
                    "type": _qtype("int (int)"),
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "CallExpr",
                                    "loc": _loc(2, source_path.as_posix()),
                                    "type": _qtype("int"),
                                    "inner": [
                                        {
                                            "kind": "DeclRefExpr",
                                            "name": "pg_atomic_compare_exchange_u32",
                                            "loc": _loc(2, source_path.as_posix()),
                                            "type": _qtype("int (int)"),
                                            "referencedDecl": {
                                                "kind": "FunctionDecl",
                                                "name": "pg_atomic_compare_exchange_u32",
                                                "loc": included_loc(source_path, 349, 1),
                                                "type": _qtype("int (int)"),
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }

            ast_by_rel = {
                "src/backend/utils/a.c": {
                    "kind": "TranslationUnitDecl",
                    "loc": _loc(1, a.as_posix()),
                    "inner": [preceding_header_decl(), atomic_decl(a), caller_decl(a, "call_a")],
                },
                "src/backend/utils/b.c": {
                    "kind": "TranslationUnitDecl",
                    "loc": _loc(1, b.as_posix()),
                    "inner": [preceding_header_decl(), atomic_decl(b), caller_decl(b, "call_b")],
                },
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel)

            result = extractor.collect([a, b], "debug")
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([fact.to_fact_record() for fact in result.facts], result.relatives)
            callers_result = store.relation_search("callers:pg_atomic_compare_exchange_u32", limit=20)

        atomics = [
            fact
            for fact in result.facts
            if fact.fact_kind == "function" and fact.object_name == "pg_atomic_compare_exchange_u32"
        ]
        self.assertEqual(len(atomics), 1)
        self.assertEqual(atomics[0].object_source, "src/include/port/atomics.h:349")
        self.assertEqual(atomics[0].payload["canonical_source"], "src/include/port/atomics.h")
        callers = {fact.object_name: fact for fact in result.facts if fact.object_name in {"call_a", "call_b"}}
        direct_calls = [relative for relative in result.relatives if relative.relation_kind == "direct_call"]
        self.assertEqual(len(direct_calls), 2)
        self.assertEqual({relative.from_fact_id for relative in direct_calls}, {callers["call_a"].object_id, callers["call_b"].object_id})
        self.assertEqual({relative.to_fact_id for relative in direct_calls}, {atomics[0].object_id})
        self.assertEqual(callers_result.status, "ok")
        self.assertEqual(callers_result.total, 2)
        self.assertEqual(callers_result.anchor.object_id, atomics[0].object_id)
        self.assertEqual({match.fact.object_name for match in callers_result.matches}, {"call_a", "call_b"})

    def test_header_inline_uses_child_header_context_without_prior_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "src" / "include" / "port" / "atomics.h"
            a = target / "src" / "backend" / "utils" / "a.c"
            b = target / "src" / "backend" / "utils" / "b.c"
            _write(header, "static inline int pg_atomic_compare_exchange_u32(int x) { return x; }\n")
            _write(a, '#include "../../include/port/atomics.h"\nint call_a(int x) { return pg_atomic_compare_exchange_u32(x); }\n')
            _write(b, '#include "../../include/port/atomics.h"\nint call_b(int x) { return pg_atomic_compare_exchange_u32(x); }\n')
            header_file = header.as_posix()

            def included_loc(source_path: Path, line: int, col: int):
                return {
                    "line": line,
                    "col": col,
                    "includedFrom": {"file": source_path.as_posix()},
                }

            def header_loc(line: int, col: int):
                return {
                    "line": line,
                    "col": col,
                    "file": header_file,
                }

            def atomic_decl(source_path: Path):
                return {
                    "kind": "FunctionDecl",
                    "name": "pg_atomic_compare_exchange_u32",
                    "loc": included_loc(source_path, 349, 1),
                    "range": {
                        "begin": included_loc(source_path, 349, 1),
                        "end": included_loc(source_path, 349, 68),
                    },
                    "type": _qtype("int (int)"),
                    "storageClass": "static",
                    "inline": True,
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "ParmVarDecl",
                            "name": "x",
                            "loc": header_loc(349, 60),
                            "type": _qtype("int"),
                        },
                        {
                            "kind": "CompoundStmt",
                            "range": {
                                "begin": included_loc(source_path, 349, 63),
                                "end": included_loc(source_path, 349, 68),
                            },
                            "inner": [],
                        },
                    ],
                }

            def caller_decl(source_path: Path, name: str):
                return {
                    "kind": "FunctionDecl",
                    "name": name,
                    "loc": _loc(2, source_path.as_posix()),
                    "type": _qtype("int (int)"),
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [
                                {
                                    "kind": "CallExpr",
                                    "loc": _loc(2, source_path.as_posix()),
                                    "type": _qtype("int"),
                                    "inner": [
                                        {
                                            "kind": "DeclRefExpr",
                                            "name": "pg_atomic_compare_exchange_u32",
                                            "loc": _loc(2, source_path.as_posix()),
                                            "type": _qtype("int (int)"),
                                            "referencedDecl": {
                                                "kind": "FunctionDecl",
                                                "name": "pg_atomic_compare_exchange_u32",
                                                "loc": included_loc(source_path, 349, 1),
                                                "type": _qtype("int (int)"),
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }

            ast_by_rel = {
                "src/backend/utils/a.c": {
                    "kind": "TranslationUnitDecl",
                    "loc": _loc(1, a.as_posix()),
                    "inner": [atomic_decl(a), caller_decl(a, "call_a")],
                },
                "src/backend/utils/b.c": {
                    "kind": "TranslationUnitDecl",
                    "loc": _loc(1, b.as_posix()),
                    "inner": [atomic_decl(b), caller_decl(b, "call_b")],
                },
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel)

            result = extractor.collect([a, b], "debug")
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([fact.to_fact_record() for fact in result.facts], result.relatives)
            callers_result = store.relation_search("callers:pg_atomic_compare_exchange_u32", limit=20)

        atomics = [
            fact
            for fact in result.facts
            if fact.fact_kind == "function" and fact.object_name == "pg_atomic_compare_exchange_u32"
        ]
        self.assertEqual(len(atomics), 1)
        self.assertEqual(atomics[0].object_source, "src/include/port/atomics.h:349")
        self.assertEqual(atomics[0].payload["canonical_source"], "src/include/port/atomics.h")
        direct_calls = [relative for relative in result.relatives if relative.relation_kind == "direct_call"]
        self.assertEqual(len(direct_calls), 2)
        self.assertEqual({relative.to_fact_id for relative in direct_calls}, {atomics[0].object_id})
        self.assertEqual(callers_result.status, "ok")
        self.assertEqual(callers_result.total, 2)
        self.assertEqual(callers_result.anchor.object_id, atomics[0].object_id)

    def test_deep_tu_header_loc_file_resolves_relative_to_compile_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "src" / "include" / "port" / "atomics.h"
            source = target / "src" / "backend" / "storage" / "lmgr" / "lwlock.c"
            _write(header, "static inline int pg_atomic_compare_exchange_u32(int x) { return x; }\n")
            _write(source, '#include "../../../../src/include/port/atomics.h"\nint call_lwlock(int x) { return pg_atomic_compare_exchange_u32(x); }\n')
            _write_compile_database(
                target / "compile_commands.json",
                [
                    {
                        "directory": "src/backend/storage/lmgr",
                        "file": "lwlock.c",
                        "arguments": ["cc", "-I../../../../src/include", "lwlock.c"],
                    }
                ],
            )
            write_default_config(target, compile_database="compile_commands.json", observe=False)
            header_loc = "../../../../src/include/port/atomics.h"
            source_loc = "lwlock.c"
            ast = {
                "kind": "TranslationUnitDecl",
                "loc": _loc(1, source_loc),
                "inner": [
                    {
                        "kind": "FunctionDecl",
                        "name": "pg_atomic_compare_exchange_u32",
                        "loc": _loc(349, header_loc, 1),
                        "type": _qtype("int (int)"),
                        "storageClass": "static",
                        "inline": True,
                        "isThisDeclarationADefinition": True,
                        "inner": [{"kind": "CompoundStmt", "inner": []}],
                    },
                    {
                        "kind": "FunctionDecl",
                        "name": "call_lwlock",
                        "loc": _loc(10, source_loc, 1),
                        "type": _qtype("int (int)"),
                        "isThisDeclarationADefinition": True,
                        "inner": [
                            {
                                "kind": "CompoundStmt",
                                "inner": [
                                    {
                                        "kind": "CallExpr",
                                        "loc": _loc(10, source_loc, 32),
                                        "type": _qtype("int"),
                                        "inner": [
                                            {
                                                "kind": "DeclRefExpr",
                                                "name": "pg_atomic_compare_exchange_u32",
                                                "loc": _loc(10, source_loc, 39),
                                                "type": _qtype("int (int)"),
                                                "referencedDecl": {
                                                    "kind": "FunctionDecl",
                                                    "name": "pg_atomic_compare_exchange_u32",
                                                    "loc": _loc(349, header_loc, 1),
                                                    "type": _qtype("int (int)"),
                                                },
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
            extractor = _SyntheticAstExtractor(
                target,
                load_config(target, observe=False),
                {"src/backend/storage/lmgr/lwlock.c": ast},
            )

            result = extractor.collect([source], "debug")

        atomics = [
            fact
            for fact in result.facts
            if fact.fact_kind == "function" and fact.object_name == "pg_atomic_compare_exchange_u32"
        ]
        callers = [fact for fact in result.facts if fact.fact_kind == "function" and fact.object_name == "call_lwlock"]
        self.assertEqual(len(atomics), 1)
        self.assertEqual(atomics[0].object_source, "src/include/port/atomics.h:349")
        self.assertEqual(atomics[0].payload["canonical_source"], "src/include/port/atomics.h")
        self.assertEqual(len(callers), 1)
        self.assertEqual(callers[0].object_source, "src/backend/storage/lmgr/lwlock.c:10")
        direct_calls = [relative for relative in result.relatives if relative.relation_kind == "direct_call"]
        self.assertEqual(len(direct_calls), 1)
        self.assertEqual(direct_calls[0].from_fact_id, callers[0].object_id)
        self.assertEqual(direct_calls[0].to_fact_id, atomics[0].object_id)

    def test_header_field_identity_merges_declaration_across_translation_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "fmgr.h"
            a = target / "src" / "a.c"
            b = target / "src" / "b.c"
            c = target / "src" / "c.c"
            _write(header, "typedef struct NullableDatum { int value; } NullableDatum;\n")
            _write(a, '#include "../include/fmgr.h"\nint read_a(NullableDatum *arg) { return arg->value; }\n')
            _write(b, '#include "../include/fmgr.h"\nint read_b(NullableDatum *arg) { return arg->value; }\n')
            _write(c, '#include "../include/fmgr.h"\nint read_c(NullableDatum *arg) { return arg->value; }\n')
            header_file = header.as_posix()
            ast_by_rel = {}
            for rel_source, source_path, function_name, field_id in (
                ("src/a.c", a, "read_a", "field:nullable:a:value"),
                ("src/b.c", b, "read_b", "field:nullable:b:value"),
                ("src/c.c", c, "read_c", "field:nullable:c:value"),
            ):
                ast_by_rel[rel_source] = {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "TypedefDecl",
                            "name": "NullableDatum",
                            "loc": _loc(1, header_file, 49),
                            "type": _qtype("struct NullableDatum"),
                        },
                        {
                            "kind": "RecordDecl",
                            "name": "NullableDatum",
                            "loc": _loc(1),
                            "completeDefinition": True,
                            "type": _qtype("struct NullableDatum"),
                            "inner": [
                                {
                                    "id": field_id,
                                    "kind": "FieldDecl",
                                    "name": "value",
                                    "loc": _loc(1, col=31),
                                    "type": _qtype("int"),
                                    "ownerName": "NullableDatum",
                                }
                            ],
                        },
                        {
                            "kind": "FunctionDecl",
                            "name": function_name,
                            "loc": _loc(2, source_path.as_posix()),
                            "type": _qtype("int (NullableDatum *)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [
                                {
                                    "kind": "CompoundStmt",
                                    "inner": [
                                        {
                                            "kind": "ReturnStmt",
                                            "loc": _loc(2, source_path.as_posix()),
                                            "inner": [
                                                {
                                                    "kind": "MemberExpr",
                                                    "name": "value",
                                                    "loc": _loc(2, source_path.as_posix()),
                                                    "type": _qtype("int"),
                                                    "referencedMemberDecl": field_id,
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel)

            result = extractor.collect([a, b, c], "debug")

        fields = [fact for fact in result.facts if fact.fact_kind == "field" and fact.object_name == "value"]
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].payload["canonical_source"], "include/fmgr.h")
        self.assertEqual(fields[0].payload["owner_name"], "NullableDatum")
        readers = [relative for relative in result.relatives if relative.relation_kind == "field_read"]
        self.assertEqual({relative.to_fact_id for relative in readers}, {fields[0].object_id})
        reader_names = {
            fact.object_name
            for fact in result.facts
            if fact.object_id in {relative.from_fact_id for relative in readers}
        }
        self.assertEqual(reader_names, {"read_a", "read_b", "read_c"})

    def test_header_field_identity_ignores_included_from_when_loc_file_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "src" / "include" / "postgres.h"
            a = target / "src" / "backend" / "utils" / "adt" / "acl.c"
            b = target / "src" / "backend" / "utils" / "adt" / "enum.c"
            c = target / "src" / "backend" / "utils" / "adt" / "regexp.c"
            _write(header, "typedef struct NullableDatum { int value; } NullableDatum;\n")
            for source in (a, b, c):
                _write(source, '#include "../../../include/postgres.h"\n')
            ast_by_rel = {}

            def real_style_loc(source_path: Path, line: int, col: int):
                return {
                    "offset": 3200 + line + col,
                    "line": line,
                    "col": col,
                    "tokLen": 5,
                    "includedFrom": {"file": source_path.as_posix()},
                }

            for rel_source, source_path, function_name, field_id in (
                ("src/backend/utils/adt/acl.c", a, "read_acl", "field:nullable:acl:value"),
                ("src/backend/utils/adt/enum.c", b, "read_enum", "field:nullable:enum:value"),
                ("src/backend/utils/adt/regexp.c", c, "read_regexp", "field:nullable:regexp:value"),
            ):
                ast_by_rel[rel_source] = {
                    "kind": "TranslationUnitDecl",
                    "loc": _loc(1, source_path.as_posix()),
                    "inner": [
                        {
                            "kind": "RecordDecl",
                            "name": "NullableDatum",
                            "loc": real_style_loc(source_path, 84, 16),
                            "completeDefinition": True,
                            "type": _qtype("struct NullableDatum"),
                            "inner": [
                                {
                                    "id": field_id,
                                    "kind": "FieldDecl",
                                    "name": "value",
                                    "loc": real_style_loc(source_path, 84, 37),
                                    "type": _qtype("int"),
                                    "ownerName": "NullableDatum",
                                }
                            ],
                        },
                        {
                            "kind": "FunctionDecl",
                            "name": function_name,
                            "loc": _loc(2, source_path.as_posix()),
                            "type": _qtype("int (NullableDatum *)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [
                                {
                                    "kind": "CompoundStmt",
                                    "inner": [
                                        {
                                            "kind": "ReturnStmt",
                                            "loc": _loc(2, source_path.as_posix()),
                                            "inner": [
                                                {
                                                    "kind": "MemberExpr",
                                                    "name": "value",
                                                    "loc": _loc(2, source_path.as_posix()),
                                                    "type": _qtype("int"),
                                                    "referencedMemberDecl": field_id,
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel)

            result = extractor.collect([a, b, c], "debug")

        fields = [fact for fact in result.facts if fact.fact_kind == "field" and fact.object_name == "value"]
        self.assertEqual(len(fields), 1)
        tu_sources = {str(path.relative_to(target)) for path in (a, b, c)}
        self.assertNotIn(fields[0].payload["canonical_source"], tu_sources)
        self.assertTrue(str(fields[0].payload["canonical_source"]).startswith("unknown-header/NullableDatum@84_16_"))
        self.assertTrue(fields[0].object_source.startswith("unknown-header/NullableDatum@84_16_"))
        has_field_targets = {
            relative.to_fact_id for relative in result.relatives if relative.relation_kind == "has_field"
        }
        self.assertEqual(has_field_targets, {fields[0].object_id})
        readers = [relative for relative in result.relatives if relative.relation_kind == "field_read"]
        self.assertEqual(len(readers), 3)
        self.assertEqual({relative.to_fact_id for relative in readers}, {fields[0].object_id})

    def test_header_field_identity_prefers_macro_expansion_location_over_spelling_define_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "fmgr.h"
            a = target / "src" / "a.c"
            b = target / "src" / "b.c"
            c = target / "src" / "c.c"
            _write(header, "typedef struct NullableDatum\n{\n    MYINT value;\n} NullableDatum;\n")
            _write(a, '#define MYINT int\n#include "../include/fmgr.h"\nint read_a(NullableDatum *arg) { return arg->value; }\n')
            _write(b, '#define MYINT int\n#include "../include/fmgr.h"\nint read_b(NullableDatum *arg) { return arg->value; }\n')
            _write(c, '#define MYINT int\n#include "../include/fmgr.h"\nint read_c(NullableDatum *arg) { return arg->value; }\n')
            header_file = header.as_posix()
            ast_by_rel = {}
            for rel_source, source_path, function_name, field_id in (
                ("src/a.c", a, "read_a", "field:nullable:a:value"),
                ("src/b.c", b, "read_b", "field:nullable:b:value"),
                ("src/c.c", c, "read_c", "field:nullable:c:value"),
            ):
                ast_by_rel[rel_source] = {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "RecordDecl",
                            "name": "NullableDatum",
                            "loc": _loc(1, header_file, 16),
                            "completeDefinition": True,
                            "type": _qtype("struct NullableDatum"),
                            "inner": [
                                {
                                    "id": field_id,
                                    "kind": "FieldDecl",
                                    "name": "value",
                                    "loc": _loc(3, col=11),
                                    "range": {
                                        "begin": {
                                            "spellingLoc": _loc(1, source_path.as_posix(), 9),
                                            "expansionLoc": _loc(3, header_file, 5),
                                        }
                                    },
                                    "type": _qtype("MYINT"),
                                    "ownerName": "NullableDatum",
                                }
                            ],
                        },
                        {
                            "kind": "TypedefDecl",
                            "name": "NullableDatum",
                            "loc": _loc(4, header_file, 3),
                            "type": _qtype("struct NullableDatum"),
                        },
                        {
                            "kind": "FunctionDecl",
                            "name": function_name,
                            "loc": _loc(3, source_path.as_posix()),
                            "type": _qtype("int (NullableDatum *)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [
                                {
                                    "kind": "CompoundStmt",
                                    "inner": [
                                        {
                                            "kind": "ReturnStmt",
                                            "loc": _loc(3, source_path.as_posix()),
                                            "inner": [
                                                {
                                                    "kind": "MemberExpr",
                                                    "name": "value",
                                                    "loc": _loc(3, source_path.as_posix()),
                                                    "type": _qtype("int"),
                                                    "referencedMemberDecl": field_id,
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel)

            result = extractor.collect([a, b, c], "debug")

        fields = [fact for fact in result.facts if fact.fact_kind == "field" and fact.object_name == "value"]
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].object_source, "include/fmgr.h:3")
        self.assertEqual(fields[0].payload["canonical_source"], "include/fmgr.h")
        readers = [relative for relative in result.relatives if relative.relation_kind == "field_read"]
        self.assertEqual({relative.to_fact_id for relative in readers}, {fields[0].object_id})
        self.assertEqual(len(readers), 3)

    def test_same_named_header_fields_remain_distinct_for_different_structs(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "types.h"
            source = target / "src" / "main.c"
            _write(header, "struct Left { int value; }; struct Right { int value; };\n")
            _write(source, '#include "../include/types.h"\n')
            header_file = header.as_posix()
            ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "RecordDecl",
                        "name": "Left",
                        "loc": _loc(1, header_file, 8),
                        "completeDefinition": True,
                        "type": _qtype("struct Left"),
                        "inner": [_field_decl("Left", "value", line=1, file=header_file)],
                    },
                    {
                        "kind": "RecordDecl",
                        "name": "Right",
                        "loc": _loc(1, header_file, 36),
                        "completeDefinition": True,
                        "type": _qtype("struct Right"),
                        "inner": [_field_decl("Right", "value", line=1, file=header_file)],
                    },
                ],
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)

            result = extractor.collect([source], "debug")

        fields = [fact for fact in result.facts if fact.fact_kind == "field" and fact.object_name == "value"]
        self.assertEqual(len(fields), 2)
        self.assertEqual({field.payload["owner_name"] for field in fields}, {"Left", "Right"})

    def test_static_same_name_functions_in_different_sources_have_distinct_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            a = target / "src" / "a.c"
            b = target / "src" / "b.c"
            _write(a, "static int helper(void) { return 1; }\n")
            _write(b, "static int helper(void) { return 2; }\n")
            ast_by_rel = {
                "src/a.c": {"kind": "TranslationUnitDecl", "inner": [{"kind": "FunctionDecl", "name": "helper", "loc": _loc(1, a.as_posix()), "type": _qtype("int (void)"), "storageClass": "static", "isThisDeclarationADefinition": True, "inner": [{"kind": "CompoundStmt", "inner": []}]}]},
                "src/b.c": {"kind": "TranslationUnitDecl", "inner": [{"kind": "FunctionDecl", "name": "helper", "loc": _loc(1, b.as_posix()), "type": _qtype("int (void)"), "storageClass": "static", "isThisDeclarationADefinition": True, "inner": [{"kind": "CompoundStmt", "inner": []}]}]},
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel)

            result = extractor.collect([a, b], "debug")

        helpers = [fact for fact in result.facts if fact.fact_kind == "function" and fact.object_name == "helper"]
        self.assertEqual(len(helpers), 2)
        self.assertEqual({fact.object_source for fact in helpers}, {"src/a.c:1", "src/b.c:1"})
        self.assertEqual(len({fact.object_id for fact in helpers}), 2)

    def test_unresolved_referenced_call_is_bounded_evidence_not_relation(self):
        ast = {
            "kind": "TranslationUnitDecl",
            "inner": [
                {
                    "kind": "FunctionDecl",
                    "name": "entry",
                    "loc": {"line": 1},
                    "type": _qtype("int (void)"),
                    "isThisDeclarationADefinition": True,
                    "inner": [
                        {
                            "kind": "CompoundStmt",
                            "inner": [_call_expr("external_helper", 2)],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "entry.c", "int entry(void) { return external_helper(); }\n")
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)

            result = extractor.collect([target / "src" / "entry.c"], "debug")

        self.assertEqual([relative for relative in result.relatives if relative.relation_kind == "direct_call"], [])
        self.assertEqual(len(result.unresolved_calls), 1)
        self.assertEqual(result.unresolved_calls[0].callee_name, "external_helper")

    def test_cross_file_direct_call_resolves_unique_header_declaration_with_condition(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            entry = target / "src" / "entry.c"
            helper = target / "src" / "helper.c"
            header = target / "include" / "api.h"
            _write(header, "int helper(void);\n")
            _write(entry, '#include "../include/api.h"\nint entry(int enabled) { if (enabled) return helper(); return 0; }\n')
            _write(helper, "int helper(void) { return 1; }\n")
            condition = {
                "kind": "branch",
                "expression": "enabled",
                "branch": "then",
                "source": "src/entry.c:3",
            }
            ast_by_rel = {
                "src/entry.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "entry",
                            "loc": _loc(1, entry.as_posix()),
                            "type": _qtype("int (int)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [
                                {
                                    "kind": "CompoundStmt",
                                    "inner": [
                                        _call_expr(
                                            "helper",
                                            3,
                                            referenced_file=header.as_posix(),
                                            condition=condition,
                                        )
                                    ],
                                }
                            ],
                        }
                    ],
                },
                "src/helper.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "helper",
                            "loc": _loc(1, helper.as_posix()),
                            "type": _qtype("int (void)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [{"kind": "CompoundStmt", "inner": []}],
                        }
                    ],
                },
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel, log_enabled=True)

            result = extractor.collect([entry, helper], "debug")
            summary = open_log(target).summarize(channel="initializer")

        entry_fact = next(fact for fact in result.facts if fact.fact_kind == "function" and fact.object_name == "entry")
        helper_fact = next(fact for fact in result.facts if fact.fact_kind == "function" and fact.object_name == "helper")
        direct_calls = [relative for relative in result.relatives if relative.relation_kind == "direct_call"]
        self.assertEqual(len(direct_calls), 1)
        direct_call = direct_calls[0]
        self.assertEqual(direct_call.from_fact_id, entry_fact.object_id)
        self.assertEqual(direct_call.to_fact_id, helper_fact.object_id)
        self.assertEqual(direct_call.evidence_source, "src/entry.c:3")
        self.assertEqual(direct_call.condition.expression, "enabled")
        self.assertEqual(direct_call.payload["callee_name"], "helper")
        self.assertEqual(direct_call.payload["referenced_source"], "include/api.h")
        self.assertEqual(direct_call.payload["resolution_strategy"], "unique_name")
        self.assertNotIn(str(target), str(direct_call.payload))
        self.assertEqual(summary.custom_counts["pending_call_count"], 1)
        self.assertEqual(summary.custom_counts["resolved_call_count"], 1)
        self.assertEqual(summary.custom_counts["relative_merge_accepted_count"], len(result.relatives))
        self.assertEqual(summary.custom_counts["relative_merge_full_parse_count"], 0)
        self.assertEqual(summary.events_by_status, {"ok": 5})
        self.assertEqual(summary.events_by_name["extractor.code.worker_pool"], 1)
        self.assertEqual(summary.events_by_name["extractor.code.relative_merge"], 1)

    def test_clang_ast_guard_nodes_populate_conditions_without_fixture_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "entry.c"
            _write(
                source,
                """
struct Context { int flags; };
int helper(void) { return 1; }
int entry(int enabled, struct Context *ctx) {
  if (enabled == 1) {
    ctx->flags = helper();
  }
  while (enabled) {
    helper();
  }
  switch (enabled) {
  case 1:
    helper();
    break;
  }
  return 0;
}
""".strip()
                + "\n",
            )
            ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "RecordDecl",
                        "name": "Context",
                        "loc": _loc(1, source.as_posix()),
                        "completeDefinition": True,
                        "inner": [_field_decl("Context", "flags", line=1, file=source.as_posix())],
                    },
                    {
                        "kind": "FunctionDecl",
                        "name": "helper",
                        "loc": _loc(2, source.as_posix()),
                        "type": _qtype("int (void)"),
                        "isThisDeclarationADefinition": True,
                        "inner": [{"kind": "CompoundStmt", "inner": []}],
                    },
                    {
                        "kind": "FunctionDecl",
                        "name": "entry",
                        "loc": _loc(3, source.as_posix()),
                        "type": _qtype("int (int, struct Context *)"),
                        "isThisDeclarationADefinition": True,
                        "inner": [
                            {
                                "kind": "CompoundStmt",
                                "inner": [
                                    {
                                        "kind": "IfStmt",
                                        "loc": _loc(4, source.as_posix()),
                                        "inner": [
                                            {
                                                "kind": "BinaryOperator",
                                                "opcode": "==",
                                                "loc": _loc(4, source.as_posix()),
                                                "inner": [
                                                    {
                                                        "kind": "DeclRefExpr",
                                                        "loc": _loc(4, source.as_posix()),
                                                        "referencedDecl": {"kind": "ParmVarDecl", "name": "enabled"},
                                                    },
                                                    {"kind": "IntegerLiteral", "value": "1", "loc": _loc(4, source.as_posix())},
                                                ],
                                            },
                                            {
                                                "kind": "CompoundStmt",
                                                "inner": [
                                                    {
                                                        "kind": "BinaryOperator",
                                                        "opcode": "=",
                                                        "loc": _loc(5, source.as_posix()),
                                                        "inner": [
                                                            _member_expr("Context", "flags", 5, file=source.as_posix()),
                                                            _call_expr("helper", 5, referenced_file=source.as_posix()),
                                                        ],
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                    {
                                        "kind": "WhileStmt",
                                        "loc": _loc(7, source.as_posix()),
                                        "inner": [
                                            {
                                                "kind": "DeclRefExpr",
                                                "loc": _loc(7, source.as_posix()),
                                                "referencedDecl": {"kind": "ParmVarDecl", "name": "enabled"},
                                            },
                                            _call_expr("helper", 8, referenced_file=source.as_posix()),
                                        ],
                                    },
                                    {
                                        "kind": "SwitchStmt",
                                        "loc": _loc(10, source.as_posix()),
                                        "inner": [
                                            {
                                                "kind": "DeclRefExpr",
                                                "loc": _loc(10, source.as_posix()),
                                                "referencedDecl": {"kind": "ParmVarDecl", "name": "enabled"},
                                            },
                                            {
                                                "kind": "CompoundStmt",
                                                "inner": [
                                                    {
                                                        "kind": "CaseStmt",
                                                        "loc": _loc(11, source.as_posix()),
                                                        "inner": [
                                                            {"kind": "IntegerLiteral", "value": "1", "loc": _loc(11, source.as_posix())},
                                                            _call_expr("helper", 12, referenced_file=source.as_posix()),
                                                        ],
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast)

            result = extractor.collect([source], "debug")

        direct_call_conditions = [
            relative.condition
            for relative in result.relatives
            if relative.relation_kind == "direct_call" and relative.condition is not None
        ]
        self.assertEqual(
            {(condition.kind, condition.expression, condition.branch) for condition in direct_call_conditions},
            {
                ("branch", "enabled == 1", "then"),
                ("loop_guard", "enabled", "body"),
                ("case", "1", "case"),
            },
        )
        field_write = next(relative for relative in result.relatives if relative.relation_kind == "field_write")
        self.assertEqual(field_write.condition.kind, "branch")
        self.assertEqual(field_write.condition.expression, "enabled == 1")

    def test_real_clang_ast_populates_guard_conditions_for_calls_and_fields(self):
        clang = shutil.which("clang")
        if clang is None:
            self.skipTest("clang is not available")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "main.c"
            _write(
                source,
                """
struct Context { int flags; };
int helper(void) { return 1; }
int fallback(void) { return 2; }
int wait_forever(void) { return 3; }
int tick_forever(void) { return 4; }
int entry(int enabled, struct Context *ctx) {
#if 1
  helper();
#endif
  if (enabled) {
    ctx->flags = helper();
  } else {
    fallback();
  }
  while (enabled) {
    helper();
    break;
  }
  for (;;) {
    wait_forever();
    break;
  }
  for (int i = 0;; i++) {
    tick_forever();
    break;
  }
  switch (enabled) {
  case 1:
    fallback();
    break;
  default:
    helper();
    break;
  }
  return 0;
}
""".strip()
                + "\n",
            )
            write_default_config(target, clang_executable=clang, gcc_executable=None, observe=False)
            extractor = CodeFactExtractor(target, load_config(target, observe=False), log_enabled=True)
            try:
                result = extractor.collect(["main.c"], "debug")
            except Exception as exc:
                if getattr(exc, "code", None) == "clang_capability_failed":
                    self.skipTest(str(exc))
                raise
            summary = open_log(target).summarize(channel="initializer")
            manifest = open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [fact.to_fact_record() for fact in result.facts],
                result.relatives,
                result.source_inventory,
            )
            storage_stats = open_fact_store(target, mode="r", log_enabled=False).stats()

        direct_call_conditions = [
            relative.condition
            for relative in result.relatives
            if relative.relation_kind == "direct_call" and relative.condition is not None
        ]
        condition_kinds = {condition.kind for condition in direct_call_conditions}
        self.assertTrue({"compile_guard", "branch", "loop_guard", "case"}.issubset(condition_kinds))
        self.assertIn(("branch", "enabled", "then"), {(item.kind, item.expression, item.branch) for item in direct_call_conditions})
        self.assertIn(("loop_guard", "enabled", "body"), {(item.kind, item.expression, item.branch) for item in direct_call_conditions})
        self.assertIn(("case", "1", "case"), {(item.kind, item.expression, item.branch) for item in direct_call_conditions})
        self.assertIn(("case", "default", "default"), {(item.kind, item.expression, item.branch) for item in direct_call_conditions})
        self.assertIn(("compile_guard", "1", "then"), {(item.kind, item.expression, item.branch) for item in direct_call_conditions})
        self.assertTrue(all(item.source and item.source.startswith("main.c:") for item in direct_call_conditions))
        facts_by_id = {fact.object_id: fact for fact in result.facts}
        unconditional_for_calls = [
            relative
            for relative in result.relatives
            if relative.relation_kind == "direct_call"
            and facts_by_id[relative.to_fact_id].object_name in {"wait_forever", "tick_forever"}
        ]
        self.assertEqual({facts_by_id[relative.to_fact_id].object_name for relative in unconditional_for_calls}, {"wait_forever", "tick_forever"})
        self.assertTrue(all(relative.condition is None for relative in unconditional_for_calls))
        field_writes = [relative for relative in result.relatives if relative.relation_kind == "field_write"]
        self.assertTrue(any(relative.condition and relative.condition.kind == "branch" for relative in field_writes))
        self.assertGreater(summary.custom_counts["conditional_relative_count"], 0)
        self.assertGreater(manifest.stats["conditional_relative_count"], 0)
        self.assertGreater(storage_stats.conditional_relative_count, 0)

    def test_cross_file_direct_call_exact_source_wins_over_ambiguous_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            entry = target / "src" / "entry.c"
            helper_a = target / "src" / "a.c"
            helper_b = target / "src" / "b.c"
            _write(entry, "int entry(void) { return helper(); }\n")
            _write(helper_a, "int helper(void) { return 1; }\n")
            _write(helper_b, "int helper(void) { return 2; }\n")
            ast_by_rel = {
                "src/entry.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "entry",
                            "loc": _loc(1, entry.as_posix()),
                            "type": _qtype("int (void)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [
                                {
                                    "kind": "CompoundStmt",
                                    "inner": [_call_expr("helper", 1, referenced_file=helper_a.as_posix())],
                                }
                            ],
                        }
                    ],
                },
                "src/a.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "helper",
                            "loc": _loc(1, helper_a.as_posix()),
                            "type": _qtype("int (void)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [{"kind": "CompoundStmt", "inner": []}],
                        }
                    ],
                },
                "src/b.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "helper",
                            "loc": _loc(1, helper_b.as_posix()),
                            "type": _qtype("int (void)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [{"kind": "CompoundStmt", "inner": []}],
                        }
                    ],
                },
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel)

            result = extractor.collect([entry, helper_a, helper_b], "debug")

        target_helper = next(
            fact
            for fact in result.facts
            if fact.fact_kind == "function" and fact.object_name == "helper" and fact.object_source == "src/a.c:1"
        )
        direct_call = next(relative for relative in result.relatives if relative.relation_kind == "direct_call")
        self.assertEqual(direct_call.to_fact_id, target_helper.object_id)
        self.assertEqual(direct_call.payload["resolution_strategy"], "exact_source")

    def test_cross_file_direct_call_ambiguous_same_name_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            entry = target / "src" / "entry.c"
            helper_a = target / "src" / "a.c"
            helper_b = target / "src" / "b.c"
            _write(entry, "int entry(void) { return helper(); }\n")
            _write(helper_a, "int helper(void) { return 1; }\n")
            _write(helper_b, "int helper(void) { return 2; }\n")
            helper_decl = {
                "kind": "FunctionDecl",
                "name": "helper",
                "type": _qtype("int (void)"),
                "isThisDeclarationADefinition": True,
                "inner": [{"kind": "CompoundStmt", "inner": []}],
            }
            ast_by_rel = {
                "src/entry.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "entry",
                            "loc": _loc(1, entry.as_posix()),
                            "type": _qtype("int (void)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [{"kind": "CompoundStmt", "inner": [_call_expr("helper", 1)]}],
                        }
                    ],
                },
                "src/a.c": {"kind": "TranslationUnitDecl", "inner": [{**helper_decl, "loc": _loc(1, helper_a.as_posix())}]},
                "src/b.c": {"kind": "TranslationUnitDecl", "inner": [{**helper_decl, "loc": _loc(1, helper_b.as_posix())}]},
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel, log_enabled=True)

            result = extractor.collect([entry, helper_a, helper_b], "debug")
            summary = open_log(target).summarize(channel="initializer")

        self.assertEqual([relative for relative in result.relatives if relative.relation_kind == "direct_call"], [])
        self.assertEqual(summary.custom_counts["ambiguous_call_count"], 1)
        self.assertEqual(summary.events_by_status["warning"], 1)

    def test_cross_file_direct_call_does_not_cross_internal_linkage(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            entry = target / "src" / "entry.c"
            static_impl = target / "src" / "static_impl.c"
            header = target / "include" / "api.h"
            _write(header, "int only_static(void);\n")
            _write(entry, '#include "../include/api.h"\nint entry(void) { return only_static(); }\n')
            _write(static_impl, "static int only_static(void) { return 1; }\n")
            ast_by_rel = {
                "src/entry.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "entry",
                            "loc": _loc(1, entry.as_posix()),
                            "type": _qtype("int (void)"),
                            "isThisDeclarationADefinition": True,
                            "inner": [
                                {
                                    "kind": "CompoundStmt",
                                    "inner": [_call_expr("only_static", 2, referenced_file=header.as_posix())],
                                }
                            ],
                        }
                    ],
                },
                "src/static_impl.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "FunctionDecl",
                            "name": "only_static",
                            "loc": _loc(1, static_impl.as_posix()),
                            "type": _qtype("int (void)"),
                            "storageClass": "static",
                            "isThisDeclarationADefinition": True,
                            "inner": [{"kind": "CompoundStmt", "inner": []}],
                        }
                    ],
                },
            }
            extractor = _SyntheticAstExtractor(target, load_config(target, observe=False), ast_by_rel, log_enabled=True)

            result = extractor.collect([entry, static_impl], "debug")
            summary = open_log(target).summarize(channel="initializer")

        self.assertEqual([relative for relative in result.relatives if relative.relation_kind == "direct_call"], [])
        self.assertEqual(summary.custom_counts["linkage_filtered_count"], 1)
        self.assertEqual(summary.custom_counts["internal_unresolved_count"], 1)
        self.assertEqual(summary.events_by_status["warning"], 1)

    def test_direct_call_resolution_dedupes_and_counts_missing_callers(self):
        function = CodeFact(
            fact_kind="function",
            object_id="code:function:helper",
            object_name="helper",
            object_description="function helper",
            object_source="src/helper.c:1",
            object_profile="debug",
            payload={"fact_kind": "function", "canonical_source": "src/helper.c", "linkage": "external"},
        )
        caller = CodeFact(
            fact_kind="function",
            object_id="code:function:entry",
            object_name="entry",
            object_description="function entry",
            object_source="src/entry.c:1",
            object_profile="debug",
            payload={"fact_kind": "function", "canonical_source": "src/entry.c", "linkage": "external"},
        )
        evidence = [
            DirectCallEvidence("code:function:entry", "helper", "src/helper.c", "src/entry.c:2"),
            DirectCallEvidence("code:function:entry", "helper", "src/helper.c", "src/entry.c:2"),
            DirectCallEvidence("missing:caller", "helper", "src/helper.c", "src/missing.c:1"),
        ]

        result = code_extractor._resolve_pending_direct_calls([function, caller], evidence, set(), "debug")

        self.assertEqual(len(result.relatives), 1)
        self.assertEqual(result.stats.resolved_call_count, 1)
        self.assertEqual(result.stats.duplicate_relation_count, 1)
        self.assertEqual(result.stats.missing_caller_count, 1)

    def test_libclang_cursor_header_cache_skips_published_header_decl_subtree(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "shared.h"
            source_a = target / "src" / "a.c"
            source_b = target / "src" / "b.c"
            _write(header, "static inline int shared(void) { return 1; }\n")
            _write(source_a, '#include "../include/shared.h"\n')
            _write(source_b, '#include "../include/shared.h"\n')
            header_decl = _FakeCursor(
                "FunctionDecl",
                name="shared",
                loc=_loc(1, header.as_posix(), 1),
                usr="usr:shared",
                type_text="int (void)",
                is_definition=True,
                children=[
                    _FakeCursor(
                        "CompoundStmt",
                        loc=_loc(1, header.as_posix(), 32),
                        children=[_FakeCursor("ReturnStmt", loc=_loc(1, header.as_posix(), 34))],
                    )
                ],
            )
            backend = object.__new__(code_extractor._LibclangAstBackend)
            backend.target_repo = target
            backend._target_repo_resolved = target.resolve(strict=False)
            backend.api = _FakeCursorApi()
            backend._header_context_local = code_extractor.threading.local()
            cache = code_extractor._HeaderMaterializationCache()
            context_a = code_extractor._HeaderMaterializationContext(cache, 0, "src/a.c", "ctx")
            root_a = _FakeCursor("TranslationUnitDecl", loc=_loc(1, source_a.as_posix()), children=[header_decl])

            with backend.header_materialization_context(context_a):
                ast_a = backend._cursor_to_ast(root_a, None, diagnostic_lines=set(), translation_unit=None)
            key = code_extractor._header_materialization_key_from_ast_node(
                target,
                target,
                "src/a.c",
                "ctx",
                ast_a["inner"][0],
            )
            cache.publish(
                producer_seq=0,
                context_hash="ctx",
                keys=[key],
                seed=code_extractor._HeaderResolverSeed(),
            )
            visible_keys, _seed = cache.visible_state(1, "ctx")
            context_b = code_extractor._HeaderMaterializationContext(
                cache,
                1,
                "src/b.c",
                "ctx",
                visible_keys=visible_keys,
            )
            root_b = _FakeCursor("TranslationUnitDecl", loc=_loc(1, source_b.as_posix()), children=[header_decl])

            with backend.header_materialization_context(context_b):
                ast_b = backend._cursor_to_ast(root_b, None, diagnostic_lines=set(), translation_unit=None)

        cached_decl = ast_b["inner"][0]
        self.assertEqual(context_b.stats.header_decl_cache_hit_count, 1)
        self.assertEqual(context_b.stats.header_decl_skipped_subtree_count, 1)
        self.assertEqual(cached_decl["kind"], "FunctionDecl")
        self.assertTrue(cached_decl["cipher2HeaderCacheHit"])
        self.assertNotIn("inner", cached_decl)

    def test_partial_ast_file_publishes_materialized_header_cache_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "shared.h"
            source_a = target / "src" / "a.c"
            source_b = target / "src" / "b.c"
            _write(header, "static inline int shared(void) { return 1; }\n")
            _write(source_a, '#include "../include/shared.h"\n')
            _write(source_b, '#include "../include/shared.h"\n')

            def header_decl():
                return _FakeCursor(
                    "FunctionDecl",
                    name="shared",
                    loc=_loc(1, header.as_posix(), 1),
                    usr="usr:shared",
                    type_text="int (void)",
                    is_definition=True,
                    children=[
                        _FakeCursor(
                            "CompoundStmt",
                            loc=_loc(1, header.as_posix(), 32),
                            children=[_FakeCursor("ReturnStmt", loc=_loc(1, header.as_posix(), 34))],
                        )
                    ],
                )

            roots = {
                "src/a.c": _FakeCursor("TranslationUnitDecl", loc=_loc(1, source_a.as_posix()), children=[header_decl()]),
                "src/b.c": _FakeCursor("TranslationUnitDecl", loc=_loc(1, source_b.as_posix()), children=[header_decl()]),
            }
            config = write_default_config(target, extractor_worker_count=1, observe=False)
            extractor = CodeFactExtractor(target, config, log_enabled=False)
            extractor._ast_backend = _FakeCursorBackend(target, roots, {"src/a.c"})
            header_cache = code_extractor._HeaderMaterializationCache()
            lookup = code_extractor._CompileCommandLookup(
                configured=False,
                matched=False,
                entry=None,
                flags=[],
                command_hash=None,
                argument_count=0,
                stripped_argument_count=0,
            )

            def work_item(seq, source, rel_source):
                return code_extractor._FileWorkItem(
                    seq=seq,
                    source=source,
                    rel_source=rel_source,
                    profile="debug",
                    source_id=code_extractor._source_id(rel_source, "debug"),
                    compile_lookup=lookup,
                )

            partial_outcome = code_extractor._run_file_work_item_with_cache(
                extractor,
                header_cache,
                work_item(0, source_a, "src/a.c"),
                publish_header_cache=True,
            )
            cached_outcome = code_extractor._run_file_work_item_with_cache(
                extractor,
                header_cache,
                work_item(1, source_b, "src/b.c"),
                publish_header_cache=True,
            )

        self.assertIsNotNone(partial_outcome.file_result)
        self.assertEqual(partial_outcome.file_result.warning_code, "clang_ast_partial")
        self.assertEqual(header_cache.entry_count(), 1)
        self.assertIsNotNone(cached_outcome.file_result)
        self.assertEqual(cached_outcome.file_result.stats.header_decl_cache_hit_count, 1)
        self.assertEqual(cached_outcome.file_result.stats.header_decl_skipped_subtree_count, 1)

    def test_partial_ast_header_body_error_does_not_publish_incomplete_header_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "shared.h"
            source_a = target / "src" / "a.c"
            source_b = target / "src" / "b.c"
            _write(header, "static inline int shared(void) { struct Inner { int value; }; return 1; }\n")
            _write(source_a, '#include "../include/shared.h"\n')
            _write(source_b, '#include "../include/shared.h"\n')

            def clean_record():
                return _FakeCursor(
                    "StructDecl",
                    name="Inner",
                    loc=_loc(1, header.as_posix(), 39),
                    usr="usr:Inner",
                    type_text="struct Inner",
                    is_definition=True,
                    children=[
                        _FakeCursor(
                            "FieldDecl",
                            name="value",
                            loc=_loc(1, header.as_posix(), 51),
                            usr="field:Inner:value",
                            type_text="int",
                        )
                    ],
                )

            def clean_header_decl():
                return _FakeCursor(
                    "FunctionDecl",
                    name="shared",
                    loc=_loc(1, header.as_posix(), 1),
                    usr="usr:shared",
                    type_text="int (void)",
                    is_definition=True,
                    children=[
                        _FakeCursor(
                            "CompoundStmt",
                            loc=_loc(1, header.as_posix(), 32),
                            children=[clean_record()],
                        )
                    ],
                )

            def partial_header_decl():
                return _FakeCursor(
                    "FunctionDecl",
                    name="shared",
                    loc=_loc(1, header.as_posix(), 1),
                    usr="usr:shared",
                    type_text="int (void)",
                    is_definition=True,
                    children=[
                        _FakeCursor(
                            "CompoundStmt",
                            loc=_loc(1, header.as_posix(), 32),
                            children=[
                                _FakeCursor(
                                    "RecoveryExpr",
                                    loc=_loc(1, header.as_posix(), 39),
                                    children=[clean_record()],
                                )
                            ],
                        )
                    ],
                )

            roots = {
                "src/a.c": _FakeCursor("TranslationUnitDecl", loc=_loc(1, source_a.as_posix()), children=[partial_header_decl()]),
                "src/b.c": _FakeCursor("TranslationUnitDecl", loc=_loc(1, source_b.as_posix()), children=[clean_header_decl()]),
            }
            config = write_default_config(target, extractor_worker_count=1, observe=False)
            extractor = CodeFactExtractor(target, config, log_enabled=False)
            extractor._ast_backend = _FakeCursorBackend(
                target,
                roots,
                {"src/a.c"},
                diagnostic_lines_by_source={"src/a.c": {1}},
            )
            lookup = code_extractor._CompileCommandLookup(
                configured=False,
                matched=False,
                entry=None,
                flags=[],
                command_hash=None,
                argument_count=0,
                stripped_argument_count=0,
            )

            def work_item(seq, source, rel_source):
                return code_extractor._FileWorkItem(
                    seq=seq,
                    source=source,
                    rel_source=rel_source,
                    profile="debug",
                    source_id=code_extractor._source_id(rel_source, "debug"),
                    compile_lookup=lookup,
                )

            baseline = code_extractor._run_file_work_item_with_cache(
                extractor,
                code_extractor._HeaderMaterializationCache(),
                work_item(1, source_b, "src/b.c"),
                publish_header_cache=False,
            )
            header_cache = code_extractor._HeaderMaterializationCache()
            partial_outcome = code_extractor._run_file_work_item_with_cache(
                extractor,
                header_cache,
                work_item(0, source_a, "src/a.c"),
                publish_header_cache=True,
            )
            entry_count_after_partial = header_cache.entry_count()
            cached_outcome = code_extractor._run_file_work_item_with_cache(
                extractor,
                header_cache,
                work_item(1, source_b, "src/b.c"),
                publish_header_cache=True,
            )

        self.assertIsNotNone(baseline.file_result)
        self.assertIsNotNone(partial_outcome.file_result)
        self.assertIsNotNone(cached_outcome.file_result)
        self.assertEqual(partial_outcome.file_result.warning_code, "clang_ast_partial")
        self.assertEqual(entry_count_after_partial, 0)
        self.assertEqual(cached_outcome.file_result.stats.header_decl_cache_hit_count, 0)
        self.assertEqual(cached_outcome.file_result.stats.header_decl_skipped_subtree_count, 0)
        self.assertEqual(
            sorted(json.dumps(fact.to_json(), sort_keys=True) for fact in baseline.file_result.facts),
            sorted(json.dumps(fact.to_json(), sort_keys=True) for fact in cached_outcome.file_result.facts),
        )

    def test_header_cache_visibility_is_fixed_per_worker_context(self):
        cache = code_extractor._HeaderMaterializationCache()
        visible_keys, _seed = cache.visible_state(1, "ctx")
        context = code_extractor._HeaderMaterializationContext(
            cache,
            1,
            "src/b.c",
            "ctx",
            visible_keys=visible_keys,
        )

        cache.publish(
            producer_seq=0,
            context_hash="ctx",
            keys=["late-header-decl"],
            seed=code_extractor._HeaderResolverSeed(),
        )

        self.assertFalse(cache.is_materialized("late-header-decl", context))
        live_context = code_extractor._HeaderMaterializationContext(cache, 1, "src/b.c", "ctx")
        self.assertTrue(cache.is_materialized("late-header-decl", live_context))

    def test_header_resolver_seed_preserves_field_relations_for_cached_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "shared.h"
            source = target / "src" / "reader.c"
            _write(header, "struct Shared { int value; };\n")
            _write(source, '#include "../include/shared.h"\nint read(struct Shared *s) { return s->value; }\n')
            type_fact = CodeFact(
                fact_kind="type",
                object_id="code:type:shared",
                object_name="Shared",
                object_description="type Shared",
                object_source="include/shared.h:1",
                object_profile="debug",
                payload={
                    "fact_kind": "type",
                    "canonical_source": "include/shared.h",
                    "line": 1,
                },
            )
            field_fact = CodeFact(
                fact_kind="field",
                object_id="code:field:shared:value",
                object_name="value",
                object_description="field value of Shared",
                object_source="include/shared.h:1",
                object_profile="debug",
                payload={
                    "fact_kind": "field",
                    "canonical_source": "include/shared.h",
                    "line": 1,
                    "owner_name": "Shared",
                    "name": "value",
                },
            )
            seed = code_extractor._HeaderResolverSeed()
            seed.add_fact(type_fact)
            seed.add_fact(field_fact)
            seed.field_by_decl_id["field:Shared:value"] = field_fact
            ast = {
                "kind": "TranslationUnitDecl",
                "inner": [
                    {
                        "kind": "RecordDecl",
                        "name": "Shared",
                        "tagUsed": "struct",
                        "loc": _loc(1, header.as_posix(), 1),
                        "type": _qtype("struct Shared"),
                        "cipher2HeaderCacheHit": True,
                    },
                    {
                        "kind": "FunctionDecl",
                        "name": "read",
                        "loc": _loc(2, source.as_posix(), 1),
                        "type": _qtype("int (struct Shared *)"),
                        "isThisDeclarationADefinition": True,
                        "inner": [
                            {
                                "kind": "CompoundStmt",
                                "inner": [
                                    {
                                        "kind": "ReturnStmt",
                                        "loc": _loc(2, source.as_posix(), 36),
                                        "inner": [_member_expr("Shared", "value", line=2, file=source.as_posix())],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
            mapper = code_extractor._ClangAstMapper(
                target,
                "src/reader.c",
                "c",
                "debug",
                "source:reader",
                header_resolver_seed=seed,
                header_context_hash="ctx",
            )

            result = mapper.map(ast)

        relation_kinds = {relative.relation_kind for relative in result.relatives}
        self.assertIn("has_field", relation_kinds)
        self.assertIn("field_read", relation_kinds)
        self.assertIn(field_fact.object_id, {relative.to_fact_id for relative in result.relatives})


class _SyntheticAstExtractor(CodeFactExtractor):
    def __init__(self, target_repo: Path, config, ast, *, log_enabled: bool = False):
        super().__init__(target_repo, config, log_enabled=log_enabled)
        self._ast = ast

    def _validate_toolchain(self) -> None:
        self.toolchain_probe_result = code_extractor.ToolchainProbeResult(
            clang_executable="synthetic-clang",
            clang_vendor="llvm",
            clang_version="16.0.0",
            ast_json_supported=False,
            type_driven_ast=True,
            loc_file_supported=True,
            call_reference_supported=True,
            member_reference_supported=True,
            qual_type_supported=True,
            ast_root_kind="TranslationUnitDecl",
            gcc_required=False,
            gcc_checked=False,
            backend="libclang",
            libclang_library="synthetic-libclang",
            libclang_library_scope="test",
            libclang_version="16.0.0",
            version_match=True,
        )
        self._ast_backend = _SyntheticAstBackend(self._ast)


def _extraction_signature(result):
    return {
        "facts": sorted(json.dumps(fact.to_fact_record().to_json(), sort_keys=True) for fact in result.facts),
        "relatives": sorted(json.dumps(relative.to_json(), sort_keys=True) for relative in result.relatives),
        "source_inventory": sorted(json.dumps(entry.to_json(), sort_keys=True) for entry in result.source_inventory),
        "errors": [(error.code, error.source) for error in result.errors],
    }


class _FakeCursor:
    def __init__(
        self,
        kind,
        *,
        name="",
        loc=None,
        usr="",
        type_text=None,
        is_definition=False,
        linkage=None,
        children=None,
    ):
        self.kind = kind
        self.name = name
        self.loc = loc or {}
        self.usr = usr
        self.type_text = type_text
        self.is_definition = is_definition
        self.linkage = linkage
        self.children = list(children or [])
        self.parent = None
        for child in self.children:
            child.parent = self


class _FakeCursorApi:
    def cursor_kind(self, cursor):
        return cursor.kind

    def cursor_spelling(self, cursor):
        return cursor.name

    def cursor_usr(self, cursor):
        return cursor.usr

    def cursor_type_spelling(self, cursor):
        return (cursor.type_text, cursor.type_text) if cursor.type_text else (None, None)

    def cursor_location(self, cursor):
        return dict(cursor.loc)

    def cursor_range_begin(self, cursor):
        return dict(cursor.loc)

    def cursor_is_definition(self, cursor):
        return cursor.is_definition

    def cursor_linkage(self, cursor):
        return cursor.linkage

    def cursor_binary_opcode(self, cursor):
        return None

    def cursor_unary_opcode(self, cursor):
        return None

    def semantic_parent(self, cursor):
        return cursor.parent

    def referenced(self, cursor):
        return None

    def children(self, cursor):
        return list(cursor.children)

    def cursor_tokens(self, translation_unit, cursor):
        return []


class _FakeCursorBackend(code_extractor._AstBackend):
    backend_name = "libclang"

    def __init__(self, target, roots, partial_sources=(), diagnostic_lines_by_source=None):
        self.roots = roots
        self.partial_sources = set(partial_sources)
        self.diagnostic_lines_by_source = {
            source: set(lines)
            for source, lines in (diagnostic_lines_by_source or {}).items()
        }
        self.delegate = object.__new__(code_extractor._LibclangAstBackend)
        self.delegate.target_repo = target
        self.delegate._target_repo_resolved = target.resolve(strict=False)
        self.delegate.api = _FakeCursorApi()
        self.delegate._header_context_local = code_extractor.threading.local()

    def header_materialization_context(self, context):
        return self.delegate.header_materialization_context(context)

    def load_ast(self, path: Path, rel_source: str, compile_lookup=None):
        ast = self.delegate._cursor_to_ast(
            self.roots[rel_source],
            None,
            diagnostic_lines=self.diagnostic_lines_by_source.get(rel_source, set()),
            translation_unit=None,
        )
        if rel_source in self.partial_sources:
            return code_extractor._AstLoadResult(
                ast=ast,
                diagnostic_kind="partial_ast",
                diagnostic_reason="diagnostic_error",
                partial=True,
                warning_code="clang_ast_partial",
            )
        return code_extractor._AstLoadResult(ast=ast)


class _SyntheticAstBackend(code_extractor._AstBackend):
    backend_name = "libclang"

    def __init__(self, ast):
        self._ast = ast

    def probe(self):
        raise AssertionError("synthetic backend is installed after probe")

    def load_ast(self, path: Path, rel_source: str, compile_lookup=None):
        if isinstance(self._ast, dict) and rel_source in self._ast:
            return code_extractor._AstLoadResult(ast=self._ast[rel_source])
        return code_extractor._AstLoadResult(ast=self._ast)


if __name__ == "__main__":
    unittest.main()

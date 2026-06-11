import pickle
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cipher2.storage as storage_module
from cipher2.config import write_default_config
from cipher2.initializer import InitError, InitSummary, initialize_repository
from cipher2.initializer.extractor import code as code_extractor
from cipher2.storage import open_fact_store
from tests.toolchain_helpers import write_fake_toolchain


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class InitializerApiTest(unittest.TestCase):
    def test_init_error_round_trips_for_process_worker_errors(self):
        error = InitError(
            "map_reduce_conflict",
            "duplicate relative id has non-idempotent payload",
            source="src/main.c",
            details={"relative_id": "rel:001"},
        )

        restored = pickle.loads(pickle.dumps(error))

        self.assertEqual(restored.code, "map_reduce_conflict")
        self.assertEqual(restored.message, "duplicate relative id has non-idempotent payload")
        self.assertEqual(restored.source, "src/main.c")
        self.assertEqual(restored.details, {"relative_id": "rel:001"})

    def test_empty_repository_writes_empty_fact_snapshot_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            summary = initialize_repository(target, log_enabled=False)

            self.assertIsInstance(summary, InitSummary)
            self.assertTrue(summary.ok)
            self.assertIsNotNone(summary.snapshot_id)
            self.assertEqual(summary.fact_count, 0)
            self.assertEqual(summary.facts_by_kind, {})
            self.assertEqual(summary.source_count, 0)
            self.assertEqual(summary.warning_count, 0)
            self.assertEqual(summary.errors, [])
            self.assertGreaterEqual(summary.duration_ms, 0.0)
            self.assertEqual(open_fact_store(target, mode="r", log_enabled=False).stats().total_facts, 0)
            self.assertTrue((target / ".cipher" / "snapshots" / "current").exists())

    def test_single_file_initialization_writes_storage_facts_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(
                target / "src" / "main.c",
                """
#define HOOK_NAME "startup"
typedef int (*handler_t)(int);
struct Device { int id; };
int global_counter = 0;
static int target(int value) { return value + 1; }
int entry(int value) {
  handler_t handler = target;
  global_counter = target(value);
  register_hook(target);
  return handler(value);
}
""".strip()
                + "\n",
            )
            write_fake_toolchain(target)

            first = initialize_repository(target, source_roots=["src/main.c"], profile="debug", log_enabled=False)
            second = initialize_repository(target, source_roots=["src/main.c"], profile="debug", log_enabled=False)

            self.assertTrue(first.ok)
            self.assertEqual(second.snapshot_id, first.snapshot_id)
            self.assertEqual(second.fact_count, first.fact_count)
            self.assertEqual(first.source_count, 1)
            self.assertEqual(first.warning_count, 0)
            self.assertGreater(first.relative_count, 0)
            facts = list(open_fact_store(target, mode="r", log_enabled=False).iter_facts())
            relatives = list(open_fact_store(target, mode="r", log_enabled=False).iter_relatives())
            kinds = {fact.payload["fact_kind"] for fact in facts}
            self.assertTrue({"code_file", "function", "global", "type", "macro", "function_pointer_slot"} <= kinds)
            relation_kinds = {relative.relation_kind for relative in relatives}
            self.assertTrue({"defines", "direct_call", "assigned_to", "dispatches_via"} <= relation_kinds)
            self.assertTrue(all(fact.object_profile == "debug" for fact in facts))
            self.assertTrue(all(fact.object_source.startswith("src/main.c:") for fact in facts))

    def test_repeated_initialization_with_header_global_keeps_snapshot_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "hooks.h"
            source_a = target / "src" / "a.c"
            source_b = target / "src" / "b.c"
            _write(header, "extern int (*get_attavgwidth_hook)(void);\n")
            _write(source_a, '#include "../include/hooks.h"\n')
            _write(source_b, '#include "../include/hooks.h"\n')
            write_fake_toolchain(target)
            write_default_config(
                target,
                clang_executable="bin/clang",
                gcc_executable="bin/gcc",
                extractor_worker_count=1,
                observe=False,
            )
            ast_by_rel = _header_global_ast_by_rel(header.as_posix(), source_b.as_posix())

            with mock.patch.object(
                code_extractor,
                "_TEST_AST_BACKEND_FACTORY",
                lambda _extractor, _clang: _SyntheticInitAstBackend(ast_by_rel),
            ):
                first = initialize_repository(target, source_roots=["src"], profile="debug", log_enabled=False)
                second = initialize_repository(target, source_roots=["src"], profile="debug", log_enabled=False)

            facts = list(open_fact_store(target, mode="r", log_enabled=False).iter_facts())

        hooks = [
            fact
            for fact in facts
            if fact.payload.get("fact_kind") == "global" and fact.object_name == "get_attavgwidth_hook"
        ]
        self.assertTrue(first.ok)
        self.assertEqual(second.snapshot_id, first.snapshot_id)
        self.assertEqual(second.fact_count, first.fact_count)
        self.assertEqual(second.relative_count, first.relative_count)
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0].payload["canonical_source"], "include/hooks.h")
        self.assertEqual(hooks[0].payload["linkage"], "extern")

    def test_initialization_streams_without_collect_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "a.c", "int b(void);\nint a(void) { return b(); }\n")
            _write(target / "src" / "b.c", "int b(void) { return 1; }\n")
            write_fake_toolchain(target)

            with mock.patch(
                "cipher2.initializer.extractor.code.CodeFactExtractor.collect",
                side_effect=AssertionError("initializer must not materialize collect()"),
            ), mock.patch.object(
                storage_module.FileFactStore,
                "_prepare_snapshot_staging",
                side_effect=AssertionError("initializer must use sorted-unique storage path"),
            ):
                summary = initialize_repository(target, source_roots=["src"], log_enabled=False)

            self.assertTrue(summary.ok)
            self.assertEqual(summary.source_count, 2)
            relatives = list(open_fact_store(target, mode="r", log_enabled=False).iter_relatives())
            self.assertIn("direct_call", {relative.relation_kind for relative in relatives})

    def test_explicit_source_roots_and_profile_limit_scanned_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "included.c", "int included(void) { return 1; }\n")
            _write(target / "src" / "ignored.c", "int ignored(void) { return 2; }\n")
            write_fake_toolchain(target)

            summary = initialize_repository(target, source_roots=["src/included.c"], profile="release", log_enabled=False)

            self.assertTrue(summary.ok)
            self.assertEqual(summary.source_count, 1)
            facts = list(open_fact_store(target, mode="r", log_enabled=False).iter_facts())
            self.assertGreater(len(facts), 0)
            self.assertTrue(all(fact.object_source.startswith("src/included.c:") for fact in facts))
            self.assertEqual({fact.object_profile for fact in facts}, {"release"})
            self.assertFalse(any("ignored" in fact.object_name for fact in facts))


def _loc(line: int, file: str):
    return {"line": line, "file": file}


def _qtype(text: str):
    return {"qualType": text}


def _header_global_decl(header_file: str):
    return {
        "kind": "VarDecl",
        "name": "get_attavgwidth_hook",
        "loc": _loc(1, header_file),
        "type": _qtype("int (*)(void)"),
        "storageClass": "extern",
    }


def _header_global_ast_by_rel(header_file: str, b_file: str):
    return {
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
                    "loc": _loc(1, b_file),
                },
                _header_global_decl(header_file),
            ],
        },
    }


class _SyntheticInitAstBackend(code_extractor._AstBackend):
    backend_name = "libclang"

    def __init__(self, ast_by_rel):
        self._ast_by_rel = ast_by_rel

    def probe(self):
        return code_extractor.ToolchainProbeResult(
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

    def load_ast(self, path: Path, rel_source: str, compile_lookup=None):
        return code_extractor._AstLoadResult(ast=self._ast_by_rel[rel_source])


if __name__ == "__main__":
    unittest.main()

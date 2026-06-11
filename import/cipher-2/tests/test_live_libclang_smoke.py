import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import cipher2.initializer.extractor.code as code_extractor
from cipher2.config import write_default_config
from cipher2.initializer import InitError, initialize_repository
from cipher2.storage import open_fact_store
from cipher2.tools.log import open_log


_LIVE_SKIP_CODES = {
    "clang_unavailable",
    "clang_capability_failed",
    "libclang_unavailable",
    "libclang_version_mismatch",
}
_REQUIRE_LIVE_VALUES = {"1", "true", "yes", "on"}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@contextmanager
def _real_libclang_backend_scope():
    previous = code_extractor._ast_backend_module._TEST_AST_BACKEND_FACTORY
    code_extractor._clear_test_libclang_backend()
    try:
        yield
    finally:
        code_extractor._TEST_AST_BACKEND_FACTORY = previous


class LiveLibclangSmokeTest(unittest.TestCase):
    def test_live_libclang_init_covers_core_fact_and_relative_shapes(self):
        clang = shutil.which("clang")
        if clang is None:
            self._skip_or_fail("clang_unavailable", "clang executable is unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self._write_live_fixture(target)
            write_default_config(
                target,
                compile_database="build/compile_commands.json",
                clang_executable=clang,
                extractor_worker_count=1,
                observe=False,
            )

            with _real_libclang_backend_scope():
                self.assertIsNone(code_extractor._ast_backend_module._TEST_AST_BACKEND_FACTORY)
                try:
                    summary = initialize_repository(target, source_roots=["src"], profile="debug")
                except InitError as exc:
                    if exc.code in _LIVE_SKIP_CODES:
                        self._skip_or_fail(exc.code, exc.message)
                    raise

            self.assertTrue(summary.ok)
            self.assertEqual(
                summary.warning_count,
                0,
                [(error.code, error.source, error.details) for error in summary.errors],
            )
            store = open_fact_store(target, mode="r", log_enabled=False)
            facts = list(store.iter_facts())
            relatives = list(store.iter_relatives())
            events = open_log(target).read_events(channel="initializer").events

        toolchain = next(event for event in events if event.event_name == "extractor.code.toolchain")
        self.assertEqual(toolchain.payload["backend"], "libclang")
        self.assertEqual(toolchain.payload["ast_json_supported"], False)
        self.assertEqual(toolchain.payload["type_driven_ast"], True)
        self.assertNotEqual(toolchain.payload.get("libclang_library_scope"), "test", toolchain.payload)

        file_events = [event for event in events if event.event_name == "extractor.code.file"]
        self.assertTrue(file_events)
        self.assertTrue(all(event.payload.get("backend") == "libclang" for event in file_events))
        self.assertTrue(any(event.counts.get("function_pointer_dispatch_count", 0) > 0 for event in file_events))

        entry_a = self._single_fact(facts, "function", "entry_a")
        inc = self._single_fact(facts, "function", "inc")
        assign_target = self._single_fact(facts, "function", "assign_target")
        value_field = self._single_fact(facts, "field", "value")
        run_field = self._single_fact(facts, "field", "run")
        local_slot = self._single_fact(facts, "function_pointer_slot", "local_slot")

        self._assert_relation(relatives, "direct_call", entry_a.object_id, inc.object_id)
        self._assert_relation(relatives, "field_read", to_fact_id=value_field.object_id)
        self._assert_relation(relatives, "field_write", to_fact_id=value_field.object_id)
        self._assert_relation(relatives, "assigned_to", run_field.object_id, assign_target.object_id)
        self._assert_relation(relatives, "assigned_to", local_slot.object_id, assign_target.object_id)
        self._assert_relation(relatives, "dispatches_via", entry_a.object_id, run_field.object_id)
        self._assert_relation(relatives, "dispatches_via", entry_a.object_id, local_slot.object_id)

        static_helpers = self._facts(facts, "function", "same_static")
        self.assertEqual({self._source_file(fact) for fact in static_helpers}, {"src/a.c", "src/b.c"})
        static_globals = self._facts(facts, "global", "static_state")
        self.assertEqual({self._source_file(fact) for fact in static_globals}, {"src/a.c", "src/b.c"})

        union_a = self._single_fact(facts, "field", "a")
        union_b = self._single_fact(facts, "field", "b")
        self.assertEqual(union_a.payload.get("owner_kind"), "anonymous")
        self.assertEqual(union_b.payload.get("owner_kind"), "anonymous")
        self.assertIn("<anonymous-union>", str(union_a.payload.get("owner_name")))
        self._assert_relation(relatives, "has_field", to_fact_id=union_a.object_id)
        self._assert_relation(relatives, "has_field", to_fact_id=union_b.object_id)
        self._assert_relation(relatives, "field_write", to_fact_id=union_a.object_id)
        self._assert_relation(relatives, "field_read", to_fact_id=union_b.object_id)

    def test_live_libclang_function_pointer_slot_relations_cover_issue_225(self):
        clang = shutil.which("clang")
        if clang is None:
            self._skip_or_fail("clang_unavailable", "clang executable is unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self._write_issue_225_fixture(target)
            write_default_config(
                target,
                compile_database="build/compile_commands.json",
                clang_executable=clang,
                extractor_worker_count=1,
                observe=False,
            )

            with _real_libclang_backend_scope():
                self.assertIsNone(code_extractor._ast_backend_module._TEST_AST_BACKEND_FACTORY)
                try:
                    summary = initialize_repository(target, source_roots=["src"], profile="debug")
                except InitError as exc:
                    if exc.code in _LIVE_SKIP_CODES:
                        self._skip_or_fail(exc.code, exc.message)
                    raise

            self.assertTrue(summary.ok)
            self.assertEqual(
                summary.warning_count,
                0,
                [(error.code, error.source, error.details) for error in summary.errors],
            )
            store = open_fact_store(target, mode="r", log_enabled=False)
            facts = list(store.iter_facts())
            relatives = list(store.iter_relatives())
            events = open_log(target).read_events(channel="initializer").events

        file_event = next(event for event in events if event.event_name == "extractor.code.file")
        self.assertEqual(file_event.counts["function_pointer_slot_count"], 2)
        self.assertEqual(file_event.counts["function_pointer_assignment_count"], 4)
        self.assertEqual(file_event.counts["function_pointer_dispatch_count"], 4)
        self.assertEqual(file_event.counts["unresolved_dispatch_function_count"], 0)

        impl = self._single_fact(facts, "function", "impl")
        use = self._single_fact(facts, "function", "use")
        file_scope_init = self._single_fact(facts, "global", "file_scope_init")
        file_scope_assigned = self._single_fact(facts, "global", "file_scope_assigned")
        slot_a = self._single_fact(facts, "function_pointer_slot", "slot_a")
        slot_b = self._single_fact(facts, "function_pointer_slot", "slot_b")

        self._assert_relation(relatives, "assigned_to", file_scope_init.object_id, impl.object_id)
        self._assert_relation(relatives, "assigned_to", file_scope_assigned.object_id, impl.object_id)
        self._assert_relation(relatives, "assigned_to", slot_a.object_id, impl.object_id)
        self._assert_relation(relatives, "assigned_to", slot_b.object_id, impl.object_id)
        self._assert_relation(relatives, "dispatches_via", use.object_id, file_scope_init.object_id)
        self._assert_relation(relatives, "dispatches_via", use.object_id, file_scope_assigned.object_id)
        self._assert_relation(relatives, "dispatches_via", use.object_id, slot_a.object_id)
        self._assert_relation(relatives, "dispatches_via", use.object_id, slot_b.object_id)

    def _skip_or_fail(self, code: str, message: str) -> None:
        if os.environ.get("CIPHER2_REQUIRE_LIVE_LIBCLANG", "").lower() in _REQUIRE_LIVE_VALUES:
            self.fail(f"live libclang smoke is required but unavailable: {code}: {message}")
        self.skipTest(f"{code}: {message}")

    def _write_live_fixture(self, target: Path) -> None:
        _write(
            target / "include" / "api.h",
            "struct Device {\n"
            "  int value;\n"
            "  int (*run)(int);\n"
            "};\n"
            "static inline int inc(int value) { return value + 1; }\n",
        )
        _write(
            target / "src" / "a.c",
            '#include "../include/api.h"\n'
            "static int static_state = 1;\n"
            "static int same_static(void) { return static_state; }\n"
            "static int assign_target(int value) { return value + same_static(); }\n"
            "int entry_a(struct Device *dev) {\n"
            "  int (*local_slot)(int) = assign_target;\n"
            "  dev->value = inc(dev->value);\n"
            "  dev->run = assign_target;\n"
            "  return local_slot(dev->value) + dev->run(dev->value);\n"
            "}\n",
        )
        _write(
            target / "src" / "b.c",
            "static int static_state = 2;\n"
            "static int same_static(void) { return static_state; }\n"
            "struct Outer { union { int a; int b; }; };\n"
            "int entry_b(struct Outer *outer) {\n"
            "  outer->a = same_static();\n"
            "  return outer->b;\n"
            "}\n",
        )
        entries = [
            {
                "directory": ".",
                "file": "../src/a.c",
                "arguments": ["cc", "-std=c11", "-I../include", "../src/a.c"],
            },
            {
                "directory": ".",
                "file": "../src/b.c",
                "arguments": ["cc", "-std=c11", "-I../include", "../src/b.c"],
            },
        ]
        _write(target / "build" / "compile_commands.json", json.dumps(entries, sort_keys=True))

    def _write_issue_225_fixture(self, target: Path) -> None:
        _write(
            target / "src" / "fp_slots.c",
            "typedef int (*fn_t)(int);\n"
            "int impl(int x) { return x; }\n"
            "fn_t file_scope_init = impl;\n"
            "fn_t file_scope_assigned;\n"
            "int use(int v) {\n"
            "  fn_t slot_a;\n"
            "  slot_a = impl;\n"
            "  int r1 = slot_a(v);\n"
            "  fn_t slot_b = impl;\n"
            "  slot_b(v);\n"
            "  file_scope_assigned = impl;\n"
            "  return r1 + file_scope_assigned(v) + file_scope_init(v);\n"
            "}\n",
        )
        entries = [
            {
                "directory": ".",
                "file": "../src/fp_slots.c",
                "arguments": ["cc", "-std=c11", "../src/fp_slots.c"],
            }
        ]
        _write(target / "build" / "compile_commands.json", json.dumps(entries, sort_keys=True))

    def _facts(self, facts, kind: str, name: str):
        return [
            fact
            for fact in facts
            if fact.payload.get("fact_kind") == kind and fact.object_name == name
        ]

    def _single_fact(self, facts, kind: str, name: str):
        matches = self._facts(facts, kind, name)
        self.assertEqual(len(matches), 1, (kind, name, [(fact.object_id, fact.object_source) for fact in matches]))
        return matches[0]

    def _assert_relation(self, relatives, kind: str, from_fact_id: str = None, to_fact_id: str = None) -> None:
        for relative in relatives:
            if relative.relation_kind != kind:
                continue
            if from_fact_id is not None and relative.from_fact_id != from_fact_id:
                continue
            if to_fact_id is not None and relative.to_fact_id != to_fact_id:
                continue
            return
        self.fail(f"missing {kind} relation from={from_fact_id!r} to={to_fact_id!r}")

    def _source_file(self, fact) -> str:
        return fact.object_source.split(":", 1)[0]


if __name__ == "__main__":
    unittest.main()

import gzip
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from unittest import mock
from pathlib import Path

import cipher2.initializer.extractor.code as code_extractor
from cipher2.config import load_config, write_default_config
from cipher2.initializer import InitError, initialize_repository
from cipher2.initializer.extractor.code import CodeFactExtractor
from cipher2.storage import open_fact_store
from cipher2.tools.log import open_log
from tests.toolchain_helpers import write_fake_toolchain


def _read_source_inventory_text(target: Path, snapshot_id: str) -> str:
    path = target / ".cipher" / "snapshots" / snapshot_id / "source_inventory.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return handle.read()


def _write(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _write_gcc(target: Path, version: str = "10.5.0") -> None:
    _write(target / "bin" / "gcc", f"#!/bin/sh\necho 'gcc (GCC) {version}'\n", executable=True)


def _write_custom_clang(target: Path, script: str) -> None:
    _write(target / "bin" / "clang", script, executable=True)


def _probe_aware_clang(*, version_output: str = "clang version 16.0.6", target_mode: str = "ok") -> str:
    return (
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        f"  echo '{version_output}'\n"
        "  exit 0\n"
        "fi\n"
        "python3 - \"$@\" <<'PY'\n"
        "import json, pathlib, re, sys\n"
        "source = None\n"
        "for arg in sys.argv[1:]:\n"
        "    if arg.endswith(('.c','.h','.cc','.cpp','.cxx','.hh','.hpp','.hxx')):\n"
        "        source = pathlib.Path(arg)\n"
        "if source is None:\n"
        "    print('{}')\n"
        "    raise SystemExit(0)\n"
        "text = source.read_text(encoding='utf-8')\n"
        "def loc(line): return {'line': line, 'file': str(source)}\n"
        "def qtype(text): return {'qualType': text}\n"
        "if 'cipher2_toolchain_probe' in text:\n"
        "    field_id = 'field:cipher2_probe_record:member'\n"
        "    field = {'id':field_id,'kind':'FieldDecl','name':'member','loc':loc(1),'type':qtype('int'),'ownerName':'cipher2_probe_record'}\n"
        "    print(json.dumps({'kind':'TranslationUnitDecl','inner':[\n"
        "        {'kind':'RecordDecl','name':'cipher2_probe_record','loc':loc(1),'completeDefinition':True,'type':qtype('struct cipher2_probe_record'),'inner':[field]},\n"
        "        {'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]},\n"
        "        {'kind':'FunctionDecl','name':'cipher2_toolchain_probe','loc':loc(3),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[{'kind':'CallExpr','loc':loc(4),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':'cipher2_probe_callee','loc':loc(4),'type':qtype('int (int)'),'referencedDecl':{'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)')}},{'kind':'MemberExpr','name':'member','loc':loc(4),'type':qtype('int'),'referencedMemberDecl':field_id}]}]}]}\n"
        "    ]}))\n"
        "    raise SystemExit(0)\n"
        f"mode = {target_mode!r}\n"
        "if 'FAIL_AST' in text or mode == 'fail':\n"
        "    print('fatal: missing private/secret_header.h from ' + str(source), file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "if mode == 'empty_inner':\n"
        "    print(json.dumps({'kind':'TranslationUnitDecl','inner':[]}))\n"
        "    raise SystemExit(0)\n"
        "if mode == 'non_json':\n"
        "    print('not json')\n"
        "    raise SystemExit(0)\n"
        "if mode == 'sleep':\n"
        "    import time\n"
        "    time.sleep(2)\n"
        "    print('{}')\n"
        "    raise SystemExit(0)\n"
        "match = re.search(r'\\b([A-Za-z_]\\w*)\\s*\\([^;]*\\)\\s*\\{', text)\n"
        "name = match.group(1) if match else 'entry'\n"
        "def function_decl(func_name, line, inner=None):\n"
        "    return {'kind':'FunctionDecl','name':func_name,'loc':loc(line),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':inner or [{'kind':'CompoundStmt','inner':[]}]}\n"
        "if mode in {'partial', 'stderr_error'}:\n"
        "    print('error: recovered after invalid generated header', file=sys.stderr)\n"
        "    print(json.dumps({'kind':'TranslationUnitDecl','inner':[function_decl(name, 1)]}))\n"
        "    raise SystemExit(1 if mode == 'partial' else 0)\n"
        "if mode == 'recovery':\n"
        "    phantom = function_decl('phantom_recovery_target', 4)\n"
        "    bad = function_decl('bad_recovery_func', 3, [{'kind':'CompoundStmt','inner':[{'kind':'CallExpr','loc':loc(5),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':'phantom_recovery_target','loc':loc(5),'type':qtype('int (void)'),'referencedDecl':{'kind':'FunctionDecl','name':'phantom_recovery_target','loc':loc(4),'type':qtype('int (void)')}}]}]}])\n"
        "    recovery = {'kind':'RecoveryExpr','loc':loc(2),'containsErrors':True,'inner':[bad, phantom]}\n"
        "    print('error: use of undeclared identifier', file=sys.stderr)\n"
        "    print(json.dumps({'kind':'TranslationUnitDecl','inner':[function_decl('survives', 1), recovery]}))\n"
        "    raise SystemExit(1)\n"
        "print(json.dumps({'kind':'TranslationUnitDecl','inner':[function_decl(name, 1)]}))\n"
        "PY\n"
    )


def _missing_type_driven_probe_clang(missing: str) -> str:
    return (
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'clang version 16.0.6'; exit 0; fi\n"
        "python3 - \"$@\" <<'PY'\n"
        "import json, pathlib, sys\n"
        "source = pathlib.Path(sys.argv[-1])\n"
        "def loc(line):\n"
        "    data = {'line': line, 'file': str(source)}\n"
        f"    if {missing!r} == 'loc.file': data.pop('file', None)\n"
        "    return data\n"
        "def qtype(text):\n"
        f"    return {{}} if {missing!r} == 'qualType' else {{'qualType': text}}\n"
        "callee_ref = {'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)')}\n"
        f"if {missing!r} == 'call_reference': callee_ref = {{}}\n"
        "field_id = 'field:cipher2_probe_record:member'\n"
        "member_expr = {'kind':'MemberExpr','name':'member','loc':loc(4),'type':qtype('int')}\n"
        f"if {missing!r} != 'member_reference': member_expr['referencedMemberDecl'] = field_id\n"
        "ast = {'kind':'TranslationUnitDecl','inner':[\n"
        "  {'kind':'RecordDecl','name':'cipher2_probe_record','loc':loc(1),'completeDefinition':True,'type':qtype('struct cipher2_probe_record'),'inner':[{'id':field_id,'kind':'FieldDecl','name':'member','loc':loc(1),'type':qtype('int'),'ownerName':'cipher2_probe_record'}]},\n"
        "  {'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]},\n"
        "  {'kind':'FunctionDecl','name':'cipher2_toolchain_probe','loc':loc(3),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[{'kind':'CallExpr','loc':loc(4),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':'cipher2_probe_callee','loc':loc(4),'type':qtype('int (int)'),'referencedDecl':callee_ref},member_expr]}]}]}\n"
        "]}\n"
        "print(json.dumps(ast))\n"
        "PY\n"
    )


class InitializerToolchainTest(unittest.TestCase):
    def test_llvm_clang_17_capability_probe_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int entry(void) { return 0; }\n")
            write_fake_toolchain(target, clang_version="17.0.6", gcc_version="10.5.0")

            result = CodeFactExtractor(target, load_config(target, observe=False)).collect(["main.c"], "default")

            self.assertGreater(len(result.facts), 0)

    def test_apple_clang_21_capability_probe_is_accepted_without_gcc(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int entry(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(version_output="Apple clang version 21.0.0"))
            write_default_config(target, clang_executable="bin/clang", gcc_executable=None, observe=False)

            result = CodeFactExtractor(target, load_config(target, observe=False)).collect(["main.c"], "default")

            self.assertGreater(len(result.facts), 0)

    def test_gcc_version_is_not_checked_on_ast_only_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int entry(void) { return 0; }\n")
            write_fake_toolchain(target, clang_version="16.0.6", gcc_version="11.4.0")

            result = CodeFactExtractor(target, load_config(target, observe=False)).collect(["main.c"], "default")

            self.assertGreater(len(result.facts), 0)

    def test_clang_capability_probe_rejects_non_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int entry(void) { return 0; }\n")
            _write_custom_clang(
                target,
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo 'clang version 16.0.6'; exit 0; fi\n"
                "echo 'not json'\n",
            )
            _write_gcc(target)
            write_default_config(target, clang_executable="bin/clang", gcc_executable="bin/gcc", observe=False)

            with self.assertRaises(InitError) as caught:
                CodeFactExtractor(target, load_config(target, observe=False)).collect(["main.c"], "default")

            self.assertEqual(caught.exception.code, "clang_capability_failed")

    def test_clang_capability_probe_rejects_missing_probe_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int entry(void) { return 0; }\n")
            _write_custom_clang(
                target,
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo 'clang version 16.0.6'; exit 0; fi\n"
                "echo '{\"kind\":\"TranslationUnitDecl\",\"inner\":[]}'\n",
            )
            _write_gcc(target)
            write_default_config(target, clang_executable="bin/clang", gcc_executable="bin/gcc", observe=False)

            with self.assertRaises(InitError) as caught:
                CodeFactExtractor(target, load_config(target, observe=False)).collect(["main.c"], "default")

            self.assertEqual(caught.exception.code, "clang_capability_failed")

    def test_clang_capability_probe_rejects_missing_type_driven_evidence(self):
        for missing in ("loc.file", "call_reference", "member_reference", "qualType"):
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)
                    _write(target / "main.c", "int entry(void) { return 0; }\n")
                    _write_custom_clang(target, _missing_type_driven_probe_clang(missing))
                    write_default_config(target, clang_executable="bin/clang", gcc_executable=None, observe=False)

                    with self.assertRaises(InitError) as caught:
                        CodeFactExtractor(target, load_config(target, observe=False)).collect(["main.c"], "default")

                    self.assertEqual(caught.exception.code, "clang_capability_failed")
                    self.assertIn(missing, caught.exception.details["missing_evidence"])

    def test_target_ast_failure_stays_distinct_from_capability_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int entry(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(target_mode="fail"))
            _write_gcc(target)
            write_default_config(target, clang_executable="bin/clang", gcc_executable="bin/gcc", observe=False)

            result = CodeFactExtractor(target, load_config(target, observe=False)).collect(["main.c"], "default")

            self.assertEqual(result.facts, [])
            self.assertEqual(result.errors[0].code, "clang_ast_failed")
            self.assertEqual(result.errors[0].source, "main.c")
            self.assertEqual(result.errors[0].details["diagnostic_kind"], "fatal")

    def test_partial_ast_returncode_error_accepts_valid_ast_with_warning_and_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int partial_ok(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(target_mode="partial"))
            write_default_config(target, clang_executable="bin/clang", gcc_executable=None, observe=False)

            summary = initialize_repository(target, source_roots=["main.c"])

            self.assertTrue(summary.ok)
            self.assertGreater(summary.fact_count, 0)
            self.assertEqual([(error.code, error.source) for error in summary.errors], [("clang_ast_partial", "main.c")])
            self.assertEqual(summary.errors[0].details["diagnostic_kind"], "partial_ast")
            self.assertEqual(summary.errors[0].details["reason"], "nonzero_exit_and_stderr_error")
            inventory = _read_source_inventory_text(target, summary.snapshot_id)
            self.assertIn("main.c", inventory)
            partial = next(
                event
                for event in open_log(target).read_events(channel="initializer").events
                if event.event_name == "extractor.code.file" and event.error_code == "clang_ast_partial"
            )
            self.assertEqual(partial.status, "warning")
            self.assertEqual(partial.payload["outcome"], "extracted_partial")
            self.assertEqual(partial.payload["diagnostic_kind"], "partial_ast")
            self.assertEqual(partial.payload["diagnostic_reason"], "nonzero_exit_and_stderr_error")
            self.assertEqual(partial.counts["partial_ast_count"], 1)
            self.assertGreater(partial.counts["fact_count"], 0)

    def test_partial_ast_stderr_error_with_zero_returncode_is_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int stderr_ok(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(target_mode="stderr_error"))
            write_default_config(target, clang_executable="bin/clang", gcc_executable=None, observe=False)

            summary = initialize_repository(target, source_roots=["main.c"])

            self.assertTrue(summary.ok)
            self.assertEqual([(error.code, error.source) for error in summary.errors], [("clang_ast_partial", "main.c")])
            self.assertEqual(summary.errors[0].details["reason"], "stderr_error")

    def test_empty_translation_unit_inner_remains_file_ast_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "empty.c", "int empty(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(target_mode="empty_inner"))
            write_default_config(target, clang_executable="bin/clang", gcc_executable=None, observe=False)

            summary = initialize_repository(target, source_roots=["empty.c"])

            self.assertTrue(summary.ok)
            self.assertEqual(summary.fact_count, 0)
            self.assertEqual([(error.code, error.source) for error in summary.errors], [("clang_ast_failed", "empty.c")])
            self.assertEqual(summary.errors[0].details["diagnostic_kind"], "malformed_ast")
            inventory = _read_source_inventory_text(target, summary.snapshot_id)
            self.assertNotIn("empty.c", inventory)

    def test_partial_ast_recovery_subtree_does_not_emit_error_facts_or_relatives(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int survives(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(target_mode="recovery"))
            write_default_config(target, clang_executable="bin/clang", gcc_executable=None, observe=False)

            result = CodeFactExtractor(target, load_config(target, observe=False)).collect(["main.c"], "default")

            names = {fact.object_name for fact in result.facts}
            self.assertIn("survives", names)
            self.assertNotIn("bad_recovery_func", names)
            self.assertNotIn("phantom_recovery_target", names)
            self.assertEqual([relative for relative in result.relatives if relative.relation_kind == "direct_call"], [])
            self.assertEqual(result.unresolved_calls, [])
            self.assertEqual([(error.code, error.source) for error in result.errors], [("clang_ast_partial", "main.c")])

    def test_file_ast_malformed_warning_records_diagnostic_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "bad.c", "int bad(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(target_mode="non_json"))
            _write_gcc(target)
            write_default_config(target, clang_executable="bin/clang", gcc_executable="bin/gcc", observe=False)

            summary = initialize_repository(target, source_roots=["bad.c"])

            self.assertTrue(summary.ok)
            self.assertEqual(summary.warning_count, 1)
            self.assertEqual(summary.errors[0].details["diagnostic_kind"], "malformed_ast")
            skipped = next(event for event in open_log(target).read_events(channel="initializer").events if event.event_name == "extractor.code.file")
            self.assertEqual(skipped.payload["diagnostic_kind"], "malformed_ast")

    def test_file_ast_timeout_warning_records_diagnostic_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "slow.c", "int slow(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(target_mode="sleep"))
            _write_gcc(target)
            write_default_config(target, clang_executable="bin/clang", gcc_executable="bin/gcc", observe=False)
            original_timeout = code_extractor.AST_COMMAND_TIMEOUT_SECONDS
            code_extractor.AST_COMMAND_TIMEOUT_SECONDS = 1
            try:
                summary = initialize_repository(target, source_roots=["slow.c"])
            finally:
                code_extractor.AST_COMMAND_TIMEOUT_SECONDS = original_timeout

            self.assertTrue(summary.ok)
            self.assertEqual(summary.warning_count, 1)
            self.assertEqual(summary.errors[0].details["diagnostic_kind"], "timeout")
            self.assertEqual(summary.errors[0].details["reason"], "timeout")
            self.assertEqual(summary.errors[0].details["timeout_seconds"], 1)
            skipped = next(event for event in open_log(target).read_events(channel="initializer").events if event.event_name == "extractor.code.file")
            self.assertEqual(skipped.payload["diagnostic_kind"], "timeout")
            self.assertEqual(skipped.payload["diagnostic_reason"], "timeout")
            self.assertEqual(skipped.payload["timeout_seconds"], 1)

    def test_ast_timeout_scales_with_source_size_and_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "large.c"
            _write(source, "0123456789")
            original_timeout = code_extractor.AST_COMMAND_TIMEOUT_SECONDS
            original_step_bytes = code_extractor.AST_COMMAND_TIMEOUT_SIZE_STEP_BYTES
            original_step_seconds = code_extractor.AST_COMMAND_TIMEOUT_SECONDS_PER_STEP
            original_max = code_extractor.AST_COMMAND_TIMEOUT_MAX_SECONDS
            code_extractor.AST_COMMAND_TIMEOUT_SECONDS = 10
            code_extractor.AST_COMMAND_TIMEOUT_SIZE_STEP_BYTES = 4
            code_extractor.AST_COMMAND_TIMEOUT_SECONDS_PER_STEP = 3
            code_extractor.AST_COMMAND_TIMEOUT_MAX_SECONDS = 15
            try:
                timeout = code_extractor._ast_command_timeout_seconds(source)
            finally:
                code_extractor.AST_COMMAND_TIMEOUT_SECONDS = original_timeout
                code_extractor.AST_COMMAND_TIMEOUT_SIZE_STEP_BYTES = original_step_bytes
                code_extractor.AST_COMMAND_TIMEOUT_SECONDS_PER_STEP = original_step_seconds
                code_extractor.AST_COMMAND_TIMEOUT_MAX_SECONDS = original_max

            self.assertEqual(timeout, 15)

    def test_file_ast_failure_is_warning_and_other_files_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "good.c", "int good(void) { return 1; }\n")
            _write(target / "bad.c", "FAIL_AST\nint bad(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(version_output="clang version 17.0.6"))
            _write_gcc(target, "11.4.0")
            write_default_config(target, clang_executable="bin/clang", gcc_executable="bin/gcc", observe=False)

            summary = initialize_repository(target, source_roots=["good.c", "bad.c"])

            self.assertTrue(summary.ok)
            self.assertGreater(summary.fact_count, 0)
            self.assertEqual(summary.warning_count, 1)
            self.assertEqual([(error.code, error.source) for error in summary.errors], [("clang_ast_failed", "bad.c")])
            inventory = _read_source_inventory_text(target, summary.snapshot_id)
            self.assertIn("good.c", inventory)
            self.assertNotIn("bad.c", inventory)

    def test_all_file_ast_failures_still_write_snapshot_with_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "a.c", "FAIL_AST\nint a(void) { return 1; }\n")
            _write(target / "b.c", "FAIL_AST\nint b(void) { return 2; }\n")
            _write_custom_clang(target, _probe_aware_clang(version_output="clang version 17.0.6"))
            _write_gcc(target)
            write_default_config(target, clang_executable="bin/clang", gcc_executable="bin/gcc", observe=False)

            summary = initialize_repository(target, source_roots=["a.c", "b.c"])

            self.assertTrue(summary.ok)
            self.assertEqual(summary.warning_count, 2)
            self.assertEqual(summary.fact_count, 0)
            self.assertTrue((target / ".cipher" / "snapshots" / summary.snapshot_id / "manifest.json").is_file())

    def test_toolchain_and_file_warning_events_are_observable_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "good.c", "int good(void) { return 1; }\n")
            _write(target / "bad.c", "FAIL_AST\nint bad(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(version_output="Apple clang version 21.0.0"))
            write_default_config(target, clang_executable="bin/clang", gcc_executable=None, observe=False)

            summary = initialize_repository(target, source_roots=["good.c", "bad.c"])

            events = open_log(target).read_events(channel="initializer").events
            toolchain = next(event for event in events if event.event_name == "extractor.code.toolchain")
            skipped = next(event for event in events if event.event_name == "extractor.code.file" and event.status == "warning")
            run = next(event for event in events if event.event_name == "initializer.run")
            self.assertEqual(toolchain.payload["clang_vendor"], "apple")
            self.assertEqual(toolchain.payload["backend"], "libclang")
            self.assertEqual(toolchain.payload["ast_json_supported"], False)
            self.assertEqual(toolchain.payload["type_driven_ast"], True)
            self.assertEqual(toolchain.payload["loc_file_supported"], True)
            self.assertEqual(toolchain.payload["call_reference_supported"], True)
            self.assertEqual(toolchain.payload["member_reference_supported"], True)
            self.assertEqual(toolchain.payload["qual_type_supported"], True)
            self.assertEqual(toolchain.payload["gcc_required"], False)
            self.assertEqual(toolchain.payload["gcc_checked"], False)
            self.assertEqual(skipped.error_code, "clang_ast_failed")
            self.assertEqual(skipped.payload["outcome"], "skipped")
            self.assertEqual(skipped.counts["warning_count"], 1)
            self.assertEqual(run.counts["warning_count"], summary.warning_count)
            serialized = str([event.to_json() for event in events])
            self.assertNotIn(str(target), serialized)
            self.assertNotIn("secret_header", serialized)

    def test_runtime_fails_closed_when_libclang_cannot_be_located(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "main.c", "int entry(void) { return 0; }\n")
            _write_custom_clang(target, _probe_aware_clang(version_output="clang version 16.0.6"))
            write_default_config(target, clang_executable="bin/clang", gcc_executable=None, observe=False)

            with mock.patch.object(code_extractor, "_TEST_AST_BACKEND_FACTORY", None), \
                 mock.patch.object(code_extractor, "_libclang_auto_candidates", return_value=[]), \
                 mock.patch("ctypes.util.find_library", return_value=None):
                with self.assertRaises(InitError) as caught:
                    CodeFactExtractor(target, load_config(target, observe=False)).collect(["main.c"], "default")

            self.assertEqual(caught.exception.code, "libclang_unavailable")
            self.assertEqual(caught.exception.details["reason"], "auto_not_found")

    def test_libclang_resolver_uses_configured_library_only_after_auto_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "libclang.so"
            library.write_text("fake", encoding="utf-8")

            with mock.patch.object(code_extractor, "_libclang_auto_candidates", return_value=[]), \
                 mock.patch("ctypes.util.find_library", return_value=None):
                path, scope = code_extractor._resolve_libclang_library("/usr/bin/clang", library)

            self.assertEqual(path, str(library))
            self.assertEqual(scope, "configured")

    def test_libclang_backend_tries_configured_library_after_auto_version_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_library = root / "auto" / "libclang.so"
            configured_library = root / "configured" / "libclang.so"
            auto_library.parent.mkdir()
            configured_library.parent.mkdir()
            auto_library.write_text("fake", encoding="utf-8")
            configured_library.write_text("fake", encoding="utf-8")

            class FakeApi:
                def __init__(self, library_path):
                    self.library_path = library_path

                def version(self):
                    if self.library_path == str(auto_library):
                        return "clang version 15.0.0"
                    return "clang version 16.0.0"

            with mock.patch.object(code_extractor, "_libclang_auto_candidates", return_value=[auto_library]), \
                 mock.patch("ctypes.util.find_library", return_value=None), \
                 mock.patch.object(code_extractor, "_CtypesLibclangApi", FakeApi), \
                 mock.patch.object(code_extractor, "_tool_version_output", return_value="clang version 16.0.0"):
                backend = code_extractor._LibclangAstBackend(
                    clang_executable="/usr/bin/clang",
                    clang_args=[],
                    target_repo=root,
                    configured_library=configured_library,
                )

            self.assertEqual(backend.library_path, str(configured_library))
            self.assertEqual(backend.library_scope, "configured")

    def test_libclang_version_mismatch_fails_without_configured_escape_hatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_library = root / "auto" / "libclang.so"
            auto_library.parent.mkdir()
            auto_library.write_text("fake", encoding="utf-8")

            class FakeApi:
                def __init__(self, _library_path):
                    pass

                def version(self):
                    return "clang version 15.0.0"

            with mock.patch.object(code_extractor, "_libclang_auto_candidates", return_value=[auto_library]), \
                 mock.patch("ctypes.util.find_library", return_value=None), \
                 mock.patch.object(code_extractor, "_CtypesLibclangApi", FakeApi), \
                 mock.patch.object(code_extractor, "_tool_version_output", return_value="clang version 16.0.0"):
                with self.assertRaises(code_extractor._LibclangVersionMismatchError):
                    code_extractor._LibclangAstBackend(
                        clang_executable="/usr/bin/clang",
                        clang_args=[],
                        target_repo=root,
                        configured_library=None,
                    )

    def test_libclang_missing_required_symbol_is_unavailable(self):
        with mock.patch("ctypes.CDLL", return_value=object()):
            with self.assertRaises(code_extractor._LibclangUnavailableError) as caught:
                code_extractor._CtypesLibclangApi("libclang.so")

        self.assertEqual(caught.exception.reason, "unsupported_symbol")

    def test_libclang_missing_optional_opcode_symbols_still_configures(self):
        optional_symbols = {
            "clang_Cursor_getBinaryOpcode",
            "clang_getBinaryOperatorKindSpelling",
            "clang_Cursor_getUnaryOpcode",
            "clang_getUnaryOperatorKindSpelling",
        }

        class FakeFunction:
            argtypes = None
            restype = None

            def __call__(self, *_args):
                raise AssertionError("optional opcode function should not be called when unavailable")

        class FakeLib:
            def __getattr__(self, name):
                if name in optional_symbols:
                    raise AttributeError(name)
                function = FakeFunction()
                setattr(self, name, function)
                return function

        with mock.patch("ctypes.CDLL", return_value=FakeLib()):
            api = code_extractor._CtypesLibclangApi("libclang.so")

        self.assertIsNone(api.cursor_binary_opcode(code_extractor._CXCursor()))
        self.assertIsNone(api.cursor_unary_opcode(code_extractor._CXCursor()))

    def test_libclang_diagnostic_reason_uses_severity(self):
        self.assertEqual(code_extractor._libclang_diagnostic_reason([(3, {"line": 1})]), "diagnostic_error")
        self.assertEqual(code_extractor._libclang_diagnostic_reason([(4, {"line": 1})]), "diagnostic_fatal")
        self.assertEqual(
            code_extractor._libclang_diagnostic_reason([(3, {"line": 1}), (4, {"line": 2})]),
            "diagnostic_error_and_fatal",
        )
        self.assertIsNone(code_extractor._libclang_diagnostic_reason([(2, {"line": 1})]))

    def test_libclang_cursor_kinds_normalize_to_json_mapper_vocabulary(self):
        cases = {
            "MemberRefExpr": "MemberExpr",
            "StructDecl": "RecordDecl",
            "UnionDecl": "RecordDecl",
            "ClassDecl": "CXXRecordDecl",
            "ParmDecl": "ParmVarDecl",
            "CXXMethod": "CXXMethodDecl",
            "UnexposedExpr": "ImplicitCastExpr",
            "FunctionDecl": "FunctionDecl",
        }

        for native, normalized in cases.items():
            with self.subTest(native=native):
                self.assertEqual(code_extractor._normalize_libclang_cursor_kind(native), normalized)

    def test_cursor_to_ast_normalizes_native_libclang_kinds_before_probe_and_mapping(self):
        class FakeCursor:
            def __init__(
                self,
                kind,
                name="",
                *,
                line=1,
                type_text="int",
                usr="",
                definition=False,
                children=None,
                referenced=None,
            ):
                self.kind = kind
                self.name = name
                self.line = line
                self.type_text = type_text
                self.usr = usr
                self.definition = definition
                self.children = list(children or [])
                self.referenced = referenced
                self.parent = None
                for child in self.children:
                    child.parent = self

        field = FakeCursor("FieldDecl", "value", line=2, usr="field:Counter:value")
        record = FakeCursor("StructDecl", "Counter", line=2, type_text="struct Counter", definition=True, children=[field])
        member = FakeCursor("MemberRefExpr", "value", line=4, referenced=field)
        function = FakeCursor(
            "FunctionDecl",
            "entry",
            line=3,
            type_text="int (void)",
            definition=True,
            children=[FakeCursor("CompoundStmt", children=[FakeCursor("UnexposedExpr", children=[member])])],
        )
        root = FakeCursor("TranslationUnitDecl", children=[record, function])

        class FakeApi:
            def cursor_kind(self, cursor):
                return cursor.kind

            def cursor_spelling(self, cursor):
                return cursor.name

            def cursor_location(self, cursor):
                return {"file": "/tmp/repo/src/main.c", "line": cursor.line, "col": 1}

            def cursor_range_begin(self, cursor):
                return self.cursor_location(cursor)

            def cursor_type_spelling(self, cursor):
                return cursor.type_text, None

            def cursor_usr(self, cursor):
                return cursor.usr

            def cursor_is_definition(self, cursor):
                return cursor.definition

            def cursor_linkage(self, _cursor):
                return None

            def cursor_binary_opcode(self, _cursor):
                return None

            def cursor_unary_opcode(self, _cursor):
                return None

            def semantic_parent(self, cursor):
                return cursor.parent

            def referenced(self, cursor):
                return cursor.referenced

            def children(self, cursor):
                return cursor.children

        backend = object.__new__(code_extractor._LibclangAstBackend)
        backend.api = FakeApi()

        ast = backend._cursor_to_ast(root, None, diagnostic_lines=set())

        record_node = ast["inner"][0]
        wrapper_node = ast["inner"][1]["inner"][0]["inner"][0]
        member_node = wrapper_node["inner"][0]
        self.assertEqual(record_node["kind"], "RecordDecl")
        self.assertEqual(record_node["libclangKind"], "StructDecl")
        self.assertEqual(record_node["tagUsed"], "struct")
        self.assertEqual(wrapper_node["kind"], "ImplicitCastExpr")
        self.assertEqual(wrapper_node["libclangKind"], "UnexposedExpr")
        self.assertEqual(member_node["kind"], "MemberExpr")
        self.assertEqual(member_node["libclangKind"], "MemberRefExpr")
        self.assertEqual(member_node["referencedMemberDecl"], "field:Counter:value")
        self.assertTrue(code_extractor._ast_has_member_reference(ast))

    def test_cursor_to_ast_prunes_external_header_subtrees_but_keeps_repo_headers(self):
        class FakeCursor:
            def __init__(self, kind, name="", *, file_path=None, line=1, children=None):
                self.kind = kind
                self.name = name
                self.file_path = file_path
                self.line = line
                self.children = list(children or [])
                self.parent = None
                for child in self.children:
                    child.parent = self

        repo_source = "/tmp/repo/src/main.c"
        repo_header = "/tmp/repo/include/local.h"
        external_header = "/usr/include/stdio.h"
        external_leaf = FakeCursor("FieldDecl", "external_field", file_path=external_header, line=11)
        external_record = FakeCursor(
            "StructDecl",
            "FILE",
            file_path=external_header,
            line=10,
            children=[external_leaf],
        )
        repo_inline = FakeCursor(
            "FunctionDecl",
            "repo_inline",
            file_path=repo_header,
            line=3,
            children=[FakeCursor("CompoundStmt", file_path=repo_header, line=3)],
        )
        repo_function = FakeCursor(
            "FunctionDecl",
            "entry",
            file_path=repo_source,
            line=5,
            children=[FakeCursor("CompoundStmt", file_path=repo_source, line=5)],
        )
        root = FakeCursor("TranslationUnitDecl", children=[external_record, repo_inline, repo_function])

        class FakeApi:
            def __init__(self):
                self.visited = []

            def cursor_kind(self, cursor):
                return cursor.kind

            def cursor_spelling(self, cursor):
                return cursor.name

            def cursor_location(self, cursor):
                location = {"line": cursor.line, "col": 1}
                if cursor.file_path is not None:
                    location["file"] = cursor.file_path
                return location

            def cursor_range_begin(self, cursor):
                return self.cursor_location(cursor)

            def cursor_type_spelling(self, _cursor):
                return "int", None

            def cursor_usr(self, _cursor):
                return ""

            def cursor_is_definition(self, _cursor):
                return False

            def cursor_linkage(self, _cursor):
                return None

            def cursor_binary_opcode(self, _cursor):
                return None

            def cursor_unary_opcode(self, _cursor):
                return None

            def semantic_parent(self, cursor):
                return cursor.parent

            def referenced(self, _cursor):
                return None

            def children(self, cursor):
                self.visited.append(cursor.name or cursor.kind)
                return cursor.children

        backend = object.__new__(code_extractor._LibclangAstBackend)
        backend.api = FakeApi()
        backend.target_repo = Path("/tmp/repo")

        ast = backend._cursor_to_ast(root, None, diagnostic_lines=set())

        names = [node.get("name") for node in code_extractor._walk_dicts(ast)]
        self.assertNotIn("FILE", names)
        self.assertNotIn("external_field", names)
        self.assertIn("repo_inline", names)
        self.assertIn("entry", names)
        self.assertNotIn("FILE", backend.api.visited)
        self.assertNotIn("external_field", backend.api.visited)
        self.assertIn("repo_inline", backend.api.visited)

    def test_libclang_probe_keeps_tempfile_ast_outside_target_repo(self):
        class FakeCursor:
            def __init__(
                self,
                kind,
                name="",
                *,
                file_path=None,
                line=1,
                type_text="int",
                usr="",
                definition=False,
                children=None,
                referenced=None,
            ):
                self.kind = kind
                self.name = name
                self.file_path = file_path
                self.line = line
                self.type_text = type_text
                self.usr = usr
                self.definition = definition
                self.children = list(children or [])
                self.referenced = referenced
                self.parent = None
                for child in self.children:
                    child.parent = self

        class FakeTranslationUnit:
            def __init__(self, root):
                self.tu = root
                self.parse_duration_ms = 1

        class FakeApi:
            def __init__(self):
                self.parsed_path = None
                self.disposed = False

            def parse_translation_unit(self, path, _args):
                self.parsed_path = Path(path)
                probe_file = str(self.parsed_path)
                field = FakeCursor("FieldDecl", "member", file_path=probe_file, line=1, usr="field:cipher2_probe_record:member")
                record = FakeCursor(
                    "StructDecl",
                    "cipher2_probe_record",
                    file_path=probe_file,
                    line=1,
                    type_text="struct cipher2_probe_record",
                    definition=True,
                    children=[field],
                )
                callee = FakeCursor(
                    "FunctionDecl",
                    "cipher2_probe_callee",
                    file_path=probe_file,
                    line=2,
                    type_text="int (int)",
                    definition=True,
                    children=[FakeCursor("CompoundStmt", file_path=probe_file, line=2)],
                )
                call = FakeCursor(
                    "CallExpr",
                    file_path=probe_file,
                    line=3,
                    type_text="int",
                    children=[
                        FakeCursor(
                            "DeclRefExpr",
                            "cipher2_probe_callee",
                            file_path=probe_file,
                            line=3,
                            type_text="int (int)",
                            referenced=callee,
                        ),
                        FakeCursor("MemberRefExpr", "member", file_path=probe_file, line=3, referenced=field),
                    ],
                )
                probe = FakeCursor(
                    "FunctionDecl",
                    "cipher2_toolchain_probe",
                    file_path=probe_file,
                    line=3,
                    type_text="int (void)",
                    definition=True,
                    children=[FakeCursor("CompoundStmt", file_path=probe_file, line=3, children=[call])],
                )
                return FakeTranslationUnit(FakeCursor("TranslationUnitDecl", children=[record, callee, probe]))

            def diagnostics(self, _tu):
                return []

            def translation_unit_cursor(self, tu):
                return tu

            def dispose_translation_unit(self, _tu):
                self.disposed = True

            def cursor_kind(self, cursor):
                return cursor.kind

            def cursor_spelling(self, cursor):
                return cursor.name

            def cursor_location(self, cursor):
                location = {"line": cursor.line, "col": 1}
                if cursor.file_path is not None:
                    location["file"] = cursor.file_path
                return location

            def cursor_range_begin(self, cursor):
                return self.cursor_location(cursor)

            def cursor_type_spelling(self, cursor):
                return cursor.type_text, None

            def cursor_usr(self, cursor):
                return cursor.usr

            def cursor_is_definition(self, cursor):
                return cursor.definition

            def cursor_linkage(self, _cursor):
                return None

            def cursor_binary_opcode(self, _cursor):
                return None

            def cursor_unary_opcode(self, _cursor):
                return None

            def semantic_parent(self, cursor):
                return cursor.parent

            def referenced(self, cursor):
                return cursor.referenced

            def children(self, cursor):
                return cursor.children

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            target.mkdir()
            backend = object.__new__(code_extractor._LibclangAstBackend)
            backend.api = FakeApi()
            backend.clang_args = []
            backend.target_repo = target
            backend._target_repo_resolved = target.resolve(strict=False)
            backend.clang_executable = "/usr/bin/clang"
            backend.clang_version_output = "Apple clang version 21.0.0"
            backend.library_path = "/tmp/libclang.dylib"
            backend.library_scope = "test"
            backend.libclang_version = "Apple clang version 21.0.0"

            result = backend.probe()

            self.assertEqual(result.backend, "libclang")
            self.assertTrue(backend.api.disposed)
            self.assertFalse(code_extractor._is_relative_to(backend.api.parsed_path.resolve(strict=False), target.resolve(strict=False)))

    def test_libclang_operator_opcode_is_derived_from_tokens_when_helper_symbols_are_missing(self):
        self.assertEqual(
            code_extractor._operator_opcode_from_tokens("UnaryOperator", ["p", "->", "x", "++"]),
            "post++",
        )
        self.assertEqual(
            code_extractor._operator_opcode_from_tokens("UnaryOperator", ["--", "p", "->", "x"]),
            "--",
        )
        self.assertEqual(
            code_extractor._operator_opcode_from_tokens("BinaryOperator", ["p", "->", "x", "=", "s"]),
            "=",
        )
        self.assertEqual(
            code_extractor._operator_opcode_from_tokens("CompoundAssignOperator", ["p", "->", "y", "+=", "s"]),
            "+=",
        )

    def test_real_libclang_backend_matches_json_oracle_for_core_c_fixture(self):
        clang = shutil.which("clang")
        if clang is None:
            self.skipTest("clang executable is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(
                target / "include" / "util.h",
                "static inline int add_one(int value) { return value + 1; }\n",
            )
            _write(
                target / "src" / "main.c",
                '#include "util.h"\n'
                "struct Counter { int value; };\n"
                "static int helper(struct Counter *counter) {\n"
                "  counter->value = add_one(counter->value);\n"
                "  return counter->value;\n"
                "}\n"
                "int entry(void) {\n"
                "  struct Counter counter;\n"
                "  counter.value = 1;\n"
                "  return helper(&counter);\n"
                "}\n",
            )
            _write(
                target / "build" / "compile_commands.json",
                json.dumps(
                    [
                        {
                            "directory": ".",
                            "file": "../src/main.c",
                            "arguments": ["cc", "-I../include", "../src/main.c"],
                        }
                    ],
                    sort_keys=True,
                ),
            )
            write_default_config(
                target,
                compile_database="build/compile_commands.json",
                clang_executable=clang,
                extractor_worker_count=1,
                observe=False,
            )

            json_factory = lambda extractor, clang_executable: code_extractor._JsonSubprocessTestBackend(
                extractor,
                clang_executable,
            )
            json_result = self._collect_or_skip_real_toolchain(
                target,
                backend_factory=json_factory,
                skip_codes={"clang_unavailable", "clang_capability_failed"},
            )
            real_result = self._collect_or_skip_real_toolchain(
                target,
                backend_factory=None,
                skip_codes={
                    "clang_unavailable",
                    "clang_capability_failed",
                    "libclang_unavailable",
                    "libclang_version_mismatch",
                },
            )

            for result in (json_result, real_result):
                facts = {(fact.fact_kind, fact.object_name) for fact in result.facts}
                self.assertIn(("function", "entry"), facts)
                self.assertIn(("function", "helper"), facts)
                self.assertIn(("function", "add_one"), facts)
                self.assertIn(("type", "Counter"), facts)
                self.assertIn(("field", "value"), facts)
                add_one = next(fact for fact in result.facts if fact.fact_kind == "function" and fact.object_name == "add_one")
                self.assertTrue(add_one.object_source.startswith("include/util.h:"), add_one.object_source)
                relation_kinds = [relative.relation_kind for relative in result.relatives]
                self.assertIn("has_field", relation_kinds)
                self.assertIn("field_read", relation_kinds)
                self.assertIn("field_write", relation_kinds)
                self.assertIn("direct_call", relation_kinds)

            json_core = self._core_fact_and_relation_shape(json_result)
            real_core = self._core_fact_and_relation_shape(real_result)
            self.assertEqual(real_core, json_core)

    def test_real_libclang_header_cache_preserves_dual_run_and_worker_parity(self):
        clang = shutil.which("clang")
        if clang is None:
            self.skipTest("clang executable is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(
                target / "include" / "shared.h",
                "struct Shared { int value; int count; };\n"
                "static inline int shared_helper(struct Shared *item) {\n"
                "  return item->value + item->count;\n"
                "}\n",
            )
            _write(
                target / "src" / "a.c",
                '#include "shared.h"\n'
                "int read_a(struct Shared *item) { return shared_helper(item); }\n",
            )
            _write(
                target / "src" / "b.c",
                '#include "shared.h"\n'
                "int read_b(struct Shared *item) { return shared_helper(item); }\n",
            )
            _write(
                target / "build" / "compile_commands.json",
                json.dumps(
                    [
                        {
                            "directory": ".",
                            "file": "../src/a.c",
                            "arguments": ["cc", "-I../include", "../src/a.c"],
                        },
                        {
                            "directory": ".",
                            "file": "../src/b.c",
                            "arguments": ["cc", "-I../include", "../src/b.c"],
                        },
                    ],
                    sort_keys=True,
                ),
            )
            write_default_config(
                target,
                compile_database="build/compile_commands.json",
                clang_executable=clang,
                extractor_worker_count=1,
                observe=False,
            )

            cache_off_wc1 = self._collect_shared_header_shape(target, worker_count=1, cache_enabled=False)
            cache_on_wc1 = self._collect_shared_header_shape(target, worker_count=1, cache_enabled=True)
            cache_on_wc2 = self._collect_shared_header_shape(target, worker_count=2, cache_enabled=True)

        self.assertEqual(cache_on_wc1, cache_off_wc1)
        self.assertEqual(cache_on_wc2, cache_on_wc1)

    def test_real_libclang_backend_preserves_field_access_parity_without_opcode_helpers(self):
        clang = shutil.which("clang")
        if clang is None:
            self.skipTest("clang executable is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(
                target / "src" / "main.c",
                "struct Pair { int x; int y; };\n"
                "static void mutate(struct Pair *p, int s) {\n"
                "  p->x = s;\n"
                "  p->y += s;\n"
                "  p->x++;\n"
                "}\n"
                "int entry(void) {\n"
                "  struct Pair pair;\n"
                "  mutate(&pair, 1);\n"
                "  return pair.x;\n"
                "}\n",
            )
            _write(
                target / "build" / "compile_commands.json",
                json.dumps(
                    [
                        {
                            "directory": ".",
                            "file": "../src/main.c",
                            "arguments": ["cc", "../src/main.c"],
                        }
                    ],
                    sort_keys=True,
                ),
            )
            write_default_config(
                target,
                compile_database="build/compile_commands.json",
                clang_executable=clang,
                extractor_worker_count=1,
                observe=False,
            )

            json_factory = lambda extractor, clang_executable: code_extractor._JsonSubprocessTestBackend(
                extractor,
                clang_executable,
            )
            json_result = self._collect_or_skip_real_toolchain(
                target,
                backend_factory=json_factory,
                skip_codes={"clang_unavailable", "clang_capability_failed"},
            )

            optional_symbols = {
                "clang_Cursor_getBinaryOpcode",
                "clang_getBinaryOperatorKindSpelling",
                "clang_Cursor_getUnaryOpcode",
                "clang_getUnaryOperatorKindSpelling",
            }
            original_optional_function = code_extractor._CtypesLibclangApi._optional_function

            def no_opcode_helpers(api, name, argtypes, restype):
                if name in optional_symbols:
                    return None
                return original_optional_function(api, name, argtypes, restype)

            with mock.patch.object(code_extractor._CtypesLibclangApi, "_optional_function", no_opcode_helpers):
                real_result = self._collect_or_skip_real_toolchain(
                    target,
                    backend_factory=None,
                    skip_codes={
                        "clang_unavailable",
                        "clang_capability_failed",
                        "libclang_unavailable",
                        "libclang_version_mismatch",
                    },
                )

            json_shape = self._field_access_shape(json_result, {"x", "y"})
            real_shape = self._field_access_shape(real_result, {"x", "y"})
            self.assertEqual(real_shape, json_shape)
            self.assertIn(("field_write", "assignment_lhs", "x", "mutate"), real_shape)
            self.assertIn(("field_read", "read_write", "y", "mutate"), real_shape)
            self.assertIn(("field_write", "read_write", "y", "mutate"), real_shape)
            self.assertIn(("field_read", "read_write", "x", "mutate"), real_shape)
            self.assertIn(("field_write", "read_write", "x", "mutate"), real_shape)

    def test_libclang_compile_flags_absolutize_path_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "build"
            compile_flags = [
                "-Iinclude",
                "-iquote",
                "quoted",
                "-isystemsysroot/include",
                "-idirafter",
                "after",
                "-FFrameworks",
                "-includeconfig.h",
                "-imacros",
                "macros.h",
                "-isysrootsdk",
                "--sysroot=sys",
                "-DVALUE=1",
            ]
            lookup = code_extractor._CompileCommandLookup(
                configured=True,
                matched=True,
                entry=code_extractor._CompileCommandEntry(
                    source_path=base / "main.c",
                    directory_path=base,
                    flags=compile_flags,
                    raw_argument_count=0,
                    sanitized_argument_count=0,
                    stripped_argument_count=0,
                    command_hash="hash",
                ),
                flags=list(compile_flags),
                command_hash="hash",
                argument_count=0,
                stripped_argument_count=0,
            )

            flags = code_extractor._libclang_absolute_compile_flags(lookup)

            self.assertIn("-I" + str((base / "include").resolve(strict=False)), flags)
            self.assertEqual(flags[flags.index("-iquote") + 1], str((base / "quoted").resolve(strict=False)))
            self.assertIn("-isystem" + str((base / "sysroot/include").resolve(strict=False)), flags)
            self.assertEqual(flags[flags.index("-idirafter") + 1], str((base / "after").resolve(strict=False)))
            self.assertIn("-F" + str((base / "Frameworks").resolve(strict=False)), flags)
            self.assertIn("-include" + str((base / "config.h").resolve(strict=False)), flags)
            self.assertEqual(flags[flags.index("-imacros") + 1], str((base / "macros.h").resolve(strict=False)))
            self.assertIn("-isysroot" + str((base / "sdk").resolve(strict=False)), flags)
            self.assertIn("--sysroot=" + str((base / "sys").resolve(strict=False)), flags)
            self.assertIn("-DVALUE=1", flags)

    def _collect_or_skip_real_toolchain(self, target: Path, *, backend_factory, skip_codes):
        with mock.patch.object(code_extractor, "_TEST_AST_BACKEND_FACTORY", backend_factory):
            try:
                return CodeFactExtractor(target, load_config(target, observe=False)).collect(["src/main.c"], "debug")
            except InitError as exc:
                if exc.code == "libclang_unavailable" and exc.details.get("reason") == "unsupported_symbol":
                    raise
                if exc.code in skip_codes:
                    self.skipTest(f"{exc.code}: {exc.message}")
                raise

    def _collect_shared_header_shape(self, target: Path, *, worker_count: int, cache_enabled: bool):
        config = replace(load_config(target, observe=False), extractor_worker_count=worker_count)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(code_extractor, "_TEST_AST_BACKEND_FACTORY", None))
            if not cache_enabled:
                stack.enter_context(
                    mock.patch.object(code_extractor._HeaderMaterializationCache, "is_materialized", return_value=False)
                )
            try:
                result = CodeFactExtractor(target, config).collect(["src"], "debug")
            except InitError as exc:
                if exc.code == "libclang_unavailable" and exc.details.get("reason") == "unsupported_symbol":
                    raise
                if exc.code in {"clang_unavailable", "clang_capability_failed", "libclang_unavailable", "libclang_version_mismatch"}:
                    self.skipTest(f"{exc.code}: {exc.message}")
                raise
        manifest = open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
            [fact.to_fact_record() for fact in result.facts],
            result.relatives,
            result.source_inventory,
        )
        return {
            "snapshot_id": manifest.snapshot_id,
            "facts": tuple(sorted(json.dumps(fact.to_fact_record().to_json(), sort_keys=True) for fact in result.facts)),
            "relatives": tuple(sorted(json.dumps(relative.to_payload(), sort_keys=True) for relative in result.relatives)),
            "source_inventory": tuple(
                sorted(json.dumps(entry.to_json(), sort_keys=True) for entry in result.source_inventory)
            ),
        }

    def _core_fact_and_relation_shape(self, result):
        facts = {
            (fact.fact_kind, fact.object_name, fact.object_source.split(":", 1)[0])
            for fact in result.facts
            if fact.fact_kind in {"function", "type", "field"}
            and fact.object_name in {"entry", "helper", "add_one", "Counter", "value"}
        }
        relation_counts = {
            kind: sum(1 for relative in result.relatives if relative.relation_kind == kind)
            for kind in {"direct_call", "has_field", "field_read", "field_write"}
        }
        return facts, relation_counts

    def _field_access_shape(self, result, field_names):
        field_names_by_id = {
            fact.object_id: fact.object_name
            for fact in result.facts
            if fact.fact_kind == "field" and fact.object_name in field_names
        }
        function_names_by_id = {
            fact.object_id: fact.object_name
            for fact in result.facts
            if fact.fact_kind == "function"
        }
        return sorted(
            (
                relative.relation_kind,
                relative.payload.get("access_context"),
                field_names_by_id[relative.to_fact_id],
                function_names_by_id.get(relative.from_fact_id, ""),
            )
            for relative in result.relatives
            if relative.relation_kind in {"field_read", "field_write"}
            and relative.to_fact_id in field_names_by_id
        )


if __name__ == "__main__":
    unittest.main()

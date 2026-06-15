# Migrated from cipher-2 tests/test_initializer_path_safety.py (M4 facts absorption acceptance).
# Rewrites per docs/proposals/m4-test-migration-map.md:
#   * cipher2.initializer.{InitError,initialize_repository} -> c2.initializer_support.
#   * cipher2.config.write_default_config -> c2.initializer_support.build_config (6-field shim).
#   * cipher2.tools.log.open_log -> the extractor's real jsonl log
#     (arbiter_engine.facts.extractor.code.open_log); the store no-op is bypassed (map §1.2 note,
#     risk #4 — extractor keeps a real log).
#   * .cipher -> .arbiter/facts (map §1.6).
#
# Red-line: the cipher-2 `.cipher/config.yml` compile-database path-escape sub-case is dropped — the
# whole cipher2.config subsystem is excluded (map §3) and arbiter has no config-file loader, so a
# `ConfigError(path_escape)` from a config file has no analog. The extractor's own source-root
# `path_escape` (the first method) is retained and exercised through the absorbed extractor.
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.facts.extractor.code import open_log
from c2.initializer_support import InitError, build_config, initialize_repository
from c2.toolchain_helpers import write_fake_toolchain


def _write(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


class InitializerPathSafetyTest(unittest.TestCase):
    def test_source_root_escape_is_rejected_without_creating_outside_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "repo"
            outside = workspace / "outside.c"
            target.mkdir()
            outside.write_text("int escaped(void) { return 0; }\n", encoding="utf-8")

            with self.assertRaises(InitError) as caught:
                initialize_repository(target, source_roots=[outside])

            self.assertEqual(caught.exception.code, "path_escape")
            self.assertFalse((workspace / ".arbiter").exists())
            event = next(item for item in open_log(target).read_events(channel="initializer").events if item.event_name == "initializer.error")
            self.assertEqual(event.error_code, "path_escape")

    def test_invalid_source_root_profile_and_log_enabled_are_structured_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            with self.assertRaises(InitError) as missing:
                initialize_repository(target, source_roots=["missing.c"], log_enabled=False)
            self.assertEqual(missing.exception.code, "invalid_source_root")

            with self.assertRaises(InitError) as bad_profile:
                initialize_repository(target, profile="", log_enabled=False)
            self.assertEqual(bad_profile.exception.code, "invalid_profile")

            with self.assertRaises(InitError) as bad_log_enabled:
                initialize_repository(target, log_enabled="yes")
            self.assertEqual(bad_log_enabled.exception.code, "invalid_log_enabled")

    def test_compile_database_malformed_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            _write(compile_db, "{not json}\n")
            config = build_config(target, compile_database="build/compile_commands.json")

            with self.assertRaises(InitError) as malformed:
                initialize_repository(target, config=config)
            self.assertEqual(malformed.exception.code, "malformed_compile_database")
            event = next(item for item in open_log(target).read_events(channel="initializer").events if item.event_name == "initializer.error")
            self.assertEqual(event.error_code, "malformed_compile_database")
            self.assertNotIn("Traceback", str(event.to_json()))
            self.assertNotIn(str(target), str(event.to_json()))

    def test_file_ast_warning_is_structured_without_source_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "bad.c", "int bad(void) {\n")
            config = write_fake_toolchain(target)
            _write(
                target / "bin" / "clang",
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo 'clang version 16.0.6'; exit 0; fi\n"
                "python3 - \"$@\" <<'PY'\n"
                "import json, pathlib, sys\n"
                "source = None\n"
                "for arg in sys.argv[1:]:\n"
                "    if arg.endswith(('.c','.h','.cc','.cpp','.cxx','.hh','.hpp','.hxx')):\n"
                "        source = pathlib.Path(arg)\n"
                "text = source.read_text(encoding='utf-8') if source else ''\n"
                "def loc(line): return {'line': line, 'file': str(source)}\n"
                "def qtype(text): return {'qualType': text}\n"
                "if 'cipher2_toolchain_probe' in text:\n"
                "    field_id = 'field:cipher2_probe_record:member'\n"
                "    print(json.dumps({'kind':'TranslationUnitDecl','inner':[\n"
                "      {'kind':'RecordDecl','name':'cipher2_probe_record','loc':loc(1),'type':qtype('struct cipher2_probe_record'),'completeDefinition':True,'inner':[{'id':field_id,'kind':'FieldDecl','name':'member','loc':loc(1),'type':qtype('int'),'ownerName':'cipher2_probe_record'}]},\n"
                "      {'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]},\n"
                "      {'kind':'FunctionDecl','name':'cipher2_toolchain_probe','loc':loc(3),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[{'kind':'CallExpr','loc':loc(4),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':'cipher2_probe_callee','loc':loc(4),'type':qtype('int (int)'),'referencedDecl':{'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)')}},{'kind':'MemberExpr','name':'member','loc':loc(4),'type':qtype('int'),'referencedMemberDecl':field_id}]}]}]}\n"
                "    ]}))\n"
                "else:\n"
                "    print('{not json}')\n"
                "PY\n",
                executable=True,
            )

            summary = initialize_repository(target, config=config)

            self.assertTrue(summary.ok)
            self.assertEqual(summary.warning_count, 1)
            self.assertEqual([(error.code, error.source) for error in summary.errors], [("clang_ast_failed", "bad.c")])
            warning_event = next(item for item in open_log(target).read_events(channel="initializer").events if item.event_name == "extractor.code.file")
            self.assertEqual(warning_event.status, "warning")
            self.assertEqual(warning_event.error_code, "clang_ast_failed")
            self.assertEqual(warning_event.payload["outcome"], "skipped")
            self.assertNotIn("int bad", str(warning_event.to_json()))
            self.assertNotIn(str(target), str(warning_event.to_json()))


if __name__ == "__main__":
    unittest.main()

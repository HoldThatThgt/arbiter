import json
import tempfile
import unittest
from pathlib import Path

from cipher2.config import write_default_config
from cipher2.initializer import InitError, initialize_repository
from cipher2.storage import open_fact_store
from cipher2.tools.log import open_log
from cipher2.tools.views import build_overview


def _write(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _write_compile_database(path: Path, entries) -> None:
    _write(path, json.dumps(entries, sort_keys=True))


def _recording_clang_script(record_path: Path) -> str:
    return (
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'clang version 16.0.6'; exit 0; fi\n"
        "python3 - \"$@\" <<'PY'\n"
        "import json, pathlib, re, sys\n"
        f"record_path = pathlib.Path({str(record_path)!r})\n"
        "args = sys.argv[1:]\n"
        "source = None\n"
        "for arg in args:\n"
        "    if arg.endswith(('.c','.h','.cc','.cpp','.cxx','.hh','.hpp','.hxx')):\n"
        "        source = pathlib.Path(arg)\n"
        "text = source.read_text(encoding='utf-8') if source is not None else ''\n"
        "def loc(line): return {'line': line, 'file': str(source)}\n"
        "def qtype(text): return {'qualType': text}\n"
        "if 'cipher2_toolchain_probe' in text:\n"
        "    field_id = 'field:cipher2_probe_record:member'\n"
        "    ast = {'kind':'TranslationUnitDecl','inner':[\n"
        "      {'kind':'RecordDecl','name':'cipher2_probe_record','loc':loc(1),'completeDefinition':True,'type':qtype('struct cipher2_probe_record'),'inner':[{'id':field_id,'kind':'FieldDecl','name':'member','loc':loc(1),'type':qtype('int'),'ownerName':'cipher2_probe_record'}]},\n"
        "      {'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]},\n"
        "      {'kind':'FunctionDecl','name':'cipher2_toolchain_probe','loc':loc(3),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[{'kind':'CallExpr','loc':loc(4),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':'cipher2_probe_callee','loc':loc(4),'type':qtype('int (int)'),'referencedDecl':{'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)')}},{'kind':'MemberExpr','name':'member','loc':loc(4),'type':qtype('int'),'referencedMemberDecl':field_id}]}]}]}\n"
        "    ]}\n"
        "    print(json.dumps(ast, sort_keys=True))\n"
        "    raise SystemExit(0)\n"
        "record_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "record_path.write_text(json.dumps(args), encoding='utf-8')\n"
        "match = re.search(r'\\b([A-Za-z_]\\w*)\\s*\\([^;]*\\)\\s*\\{', text)\n"
        "name = match.group(1) if match else 'entry'\n"
        "ast = {'kind':'TranslationUnitDecl','inner':[{'kind':'FunctionDecl','name':name,'loc':loc(1),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]}]}\n"
        "print(json.dumps(ast, sort_keys=True))\n"
        "PY\n"
    )


def _cwd_sensitive_clang_script(record_path: Path) -> str:
    return (
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'clang version 16.0.6'; exit 0; fi\n"
        "python3 - \"$@\" <<'PY'\n"
        "import json, pathlib, sys\n"
        f"record_path = pathlib.Path({str(record_path)!r})\n"
        "args = sys.argv[1:]\n"
        "source = None\n"
        "for arg in args:\n"
        "    if arg.endswith(('.c','.h','.cc','.cpp','.cxx','.hh','.hpp','.hxx')):\n"
        "        source = pathlib.Path(arg)\n"
        "record_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "record_path.write_text(json.dumps({'cwd': str(pathlib.Path.cwd()), 'args': args}), encoding='utf-8')\n"
        "def loc(line): return {'line': line, 'file': str(source)}\n"
        "def qtype(text): return {'qualType': text}\n"
        "if source is not None and 'cipher2_toolchain_probe' in source.read_text(encoding='utf-8'):\n"
        "    field_id = 'field:cipher2_probe_record:member'\n"
        "    ast = {'kind':'TranslationUnitDecl','inner':[\n"
        "      {'kind':'RecordDecl','name':'cipher2_probe_record','loc':loc(1),'completeDefinition':True,'type':qtype('struct cipher2_probe_record'),'inner':[{'id':field_id,'kind':'FieldDecl','name':'member','loc':loc(1),'type':qtype('int'),'ownerName':'cipher2_probe_record'}]},\n"
        "      {'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]},\n"
        "      {'kind':'FunctionDecl','name':'cipher2_toolchain_probe','loc':loc(3),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[{'kind':'CallExpr','loc':loc(4),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':'cipher2_probe_callee','loc':loc(4),'type':qtype('int (int)'),'referencedDecl':{'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)')}},{'kind':'MemberExpr','name':'member','loc':loc(4),'type':qtype('int'),'referencedMemberDecl':field_id}]}]}]}\n"
        "    ]}\n"
        "    print(json.dumps(ast, sort_keys=True))\n"
        "    raise SystemExit(0)\n"
        "if not (pathlib.Path.cwd() / 'include' / 'config.h').exists():\n"
        "    print('missing relative include', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "ast = {'kind':'TranslationUnitDecl','inner':[{'kind':'FunctionDecl','name':'entry','loc':loc(2),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]}]}\n"
        "print(json.dumps(ast, sort_keys=True))\n"
        "PY\n"
    )


class InitializerCompileDatabaseTest(unittest.TestCase):
    def test_per_file_arguments_are_sanitized_and_ordered_after_global_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            record_path = target / "run" / "argv.json"
            _write(target / "src" / "main.c", "int entry(void) { return 0; }\n")
            _write(target / "bin" / "clang", _recording_clang_script(record_path), executable=True)
            _write_compile_database(
                target / "build" / "compile_commands.json",
                [
                    {
                        "directory": "..",
                        "file": "src/main.c",
                        "command": "cc -DWRONG=1 src/main.c",
                        "arguments": [
                            "cc",
                            "-Iinclude",
                            "-isystem",
                            "sys/include",
                            "-DVALUE=1",
                            "--target=x86_64-linux-gnu",
                            "-std=c11",
                            "-include",
                            "config.h",
                            "-o",
                            "main.o",
                            "-c",
                            "src/main.c",
                            "-Xclang",
                            "-load",
                            "plugin.so",
                            "-fplugin=evil.so",
                            "-O3",
                        ],
                    }
                ],
            )
            write_default_config(
                target,
                compile_database="build/compile_commands.json",
                clang_executable="bin/clang",
                clang_args=["-DGLOBAL=1"],
                observe=False,
            )

            summary = initialize_repository(target, source_roots=["src/main.c"])

            self.assertTrue(summary.ok)
            argv = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertLess(argv.index("-DGLOBAL=1"), argv.index("-DVALUE=1"))
            self.assertLess(argv.index("-DVALUE=1"), argv.index("-ferror-limit=0"))
            self.assertLess(argv.index("-std=c11"), argv.index("-ferror-limit=0"))
            self.assertLess(argv.index("-ferror-limit=0"), argv.index("-Xclang"))
            self.assertEqual(Path(argv[-1]).resolve(strict=False), (target / "src" / "main.c").resolve(strict=False))
            self.assertIn("-Iinclude", argv)
            self.assertIn("-isystem", argv)
            self.assertIn("sys/include", argv)
            self.assertIn("--target=x86_64-linux-gnu", argv)
            self.assertIn("-include", argv)
            self.assertIn("config.h", argv)
            self.assertNotIn("-DWRONG=1", argv)
            self.assertNotIn("-o", argv)
            self.assertNotIn("main.o", argv)
            self.assertNotIn("-c", argv)
            self.assertNotIn("-load", argv)
            self.assertNotIn("plugin.so", argv)
            self.assertNotIn("-fplugin=evil.so", argv)
            self.assertNotIn("-O3", argv)

    def test_compile_database_ast_invocation_runs_from_entry_directory_for_relative_includes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            record_path = target / "run" / "cwd.json"
            _write(target / "src" / "main.c", '#include "config.h"\nint entry(void) { return VALUE; }\n')
            _write(target / "build" / "include" / "config.h", "#define VALUE 1\n")
            _write(target / "bin" / "clang", _cwd_sensitive_clang_script(record_path), executable=True)
            _write_compile_database(
                target / "compile_commands.json",
                [
                    {
                        "directory": "build",
                        "file": "../src/main.c",
                        "arguments": ["cc", "-Iinclude", "../src/main.c"],
                    }
                ],
            )
            write_default_config(
                target,
                compile_database="compile_commands.json",
                clang_executable="bin/clang",
                observe=False,
            )

            summary = initialize_repository(target, source_roots=["src/main.c"])

            self.assertTrue(summary.ok)
            invocation = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(Path(invocation["cwd"]).resolve(strict=False), (target / "build").resolve(strict=False))
            facts = list(open_fact_store(target, mode="r", log_enabled=False).iter_facts())
            self.assertIn("entry", [fact.object_name for fact in facts])

    def test_compile_database_limits_sources_to_indexed_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "repo"
            outside = workspace / "outside.c"
            _write(target / "include" / "a.h", "#define A_VALUE 1\n")
            _write(target / "src" / "a.c", '#include "../include/a.h"\nint alpha(void) { return A_VALUE; }\n')
            _write(target / "src" / "b.c", "int beta(void) { return 2; }\n")
            _write(outside, "int outside(void) { return 3; }\n")
            _write(target / "bin" / "clang", _recording_clang_script(target / "run" / "argv.json"), executable=True)
            _write_compile_database(
                target / "build" / "compile_commands.json",
                [
                    {"directory": "..", "file": "src/a.c", "arguments": ["cc", "-DA=1", "-o", "a.o"]},
                    {"directory": "..", "file": "src/a.c", "arguments": ["cc", "-DA=2"]},
                    {"directory": str(workspace), "file": "outside.c", "arguments": ["cc", "-DOUTSIDE=1"]},
                ],
            )
            write_default_config(
                target,
                compile_database="build/compile_commands.json",
                clang_executable="bin/clang",
                clang_args=["-DGLOBAL=1"],
                observe=False,
            )

            summary = initialize_repository(target, source_roots=["src"])

            self.assertTrue(summary.ok)
            self.assertEqual(summary.source_count, 1)
            inventory = {entry.rel_path: entry for entry in open_fact_store(target, mode="r").iter_source_inventory()}
            self.assertEqual(set(inventory), {"include/a.h", "src/a.c"})
            self.assertIsNotNone(inventory["src/a.c"].compile_command_hash)
            self.assertIsNone(inventory["include/a.h"].compile_command_hash)
            self.assertEqual(inventory["include/a.h"].source_kind, "header")
            self.assertEqual(inventory["src/a.c"].includes, [inventory["include/a.h"].source_id])
            self.assertEqual(inventory["include/a.h"].included_by, [inventory["src/a.c"].source_id])
            facts = list(open_fact_store(target, mode="r").iter_facts())
            self.assertIn("alpha", [fact.object_name for fact in facts])
            self.assertNotIn("beta", [fact.object_name for fact in facts])
            argv = json.loads((target / "run" / "argv.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(argv[-1]).resolve(strict=False), (target / "src" / "a.c").resolve(strict=False))
            events = open_log(target).read_events(channel="initializer").events
            compile_db = next(event for event in events if event.event_name == "extractor.code.compile_database")
            self.assertEqual(compile_db.status, "ok")
            self.assertEqual(compile_db.counts["compile_command_entry_count"], 3)
            self.assertEqual(compile_db.counts["compile_command_indexed_source_count"], 1)
            self.assertEqual(compile_db.counts["compile_command_duplicate_source_count"], 1)
            self.assertEqual(compile_db.counts["compile_command_ignored_outside_repo_count"], 1)
            file_counts = [
                event.counts
                for event in events
                if event.event_name == "extractor.code.file" and event.status == "ok"
            ]
            self.assertEqual(sum(counts["compile_command_hit_count"] for counts in file_counts), 1)
            self.assertEqual(sum(counts["compile_command_miss_count"] for counts in file_counts), 0)
            overview = build_overview(target, include_sections=["log"])
            self.assertEqual(overview.log.state, "ready")
            self.assertEqual(overview.log.compile_database_configured, True)
            self.assertEqual(overview.log.compile_command_hit_count, 1)
            self.assertEqual(overview.log.compile_command_miss_count, 0)
            self.assertEqual(overview.log.compile_command_duplicate_source_count, 1)
            self.assertEqual(overview.log.compile_command_ignored_outside_repo_count, 1)

    def test_malformed_compile_database_entries_fail_closed(self):
        cases = [
            [{"directory": "..", "file": "main.c", "arguments": "cc -DNAME=1"}],
            [{"directory": 123, "file": "main.c", "arguments": ["cc"]}],
            [{"directory": "..", "file": 123, "arguments": ["cc"]}],
            [{"directory": "..", "file": "main.c", "command": "cc 'unterminated"}],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)
                    _write(target / "main.c", "int entry(void) { return 0; }\n")
                    _write(target / "bin" / "clang", _recording_clang_script(target / "run" / "argv.json"), executable=True)
                    _write_compile_database(target / "build" / "compile_commands.json", entries)
                    write_default_config(
                        target,
                        compile_database="build/compile_commands.json",
                        clang_executable="bin/clang",
                        observe=False,
                    )

                    with self.assertRaises(InitError) as caught:
                        initialize_repository(target, source_roots=["main.c"])

                    self.assertEqual(caught.exception.code, "malformed_compile_database")


if __name__ == "__main__":
    unittest.main()

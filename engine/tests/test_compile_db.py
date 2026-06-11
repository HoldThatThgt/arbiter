import json
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from arbiter_engine.shared import compile_db


class CompileDBTest(unittest.TestCase):
    def test_emit_dedups_expands_response_and_normalizes_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "work tree"
            (cwd / "src").mkdir(parents=True)
            (cwd / "src" / "a.c").write_text("int a;\n", encoding="utf-8")
            rsp = cwd / "args.rsp"
            rsp.write_text(
                "-I include\n-isystem sys\n--sysroot sysroot\n-c src/a.c\n-o build/a.o\n",
                encoding="utf-8",
            )
            journal = root / "compile-journal.b1.jsonl"
            self.write_journal(
                journal,
                {
                    "argv": ["cc", "-c", "src/a.c", "-o", "build/a.o"],
                    "cwd": str(cwd),
                    "src": "src/a.c",
                    "out": "build/a.o",
                    "ts": "old",
                },
                {
                    "argv": ["cc", f"@{rsp}"],
                    "cwd": str(cwd),
                    "src": "src/a.c",
                    "out": "build/a.o",
                    "ts": "new",
                },
            )

            result = compile_db.emit([journal], root / "compile_commands.json")

            self.assertEqual(result.entries, 1)
            data = json.loads((root / "compile_commands.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data), 1)
            entry = data[0]
            self.assertEqual(entry["directory"], str(cwd))
            self.assertEqual(entry["file"], str(cwd / "src" / "a.c"))
            self.assertEqual(entry["output"], str(cwd / "build" / "a.o"))
            self.assertEqual(
                entry["arguments"],
                [
                    "cc",
                    "-I",
                    str(cwd / "include"),
                    "-isystem",
                    str(cwd / "sys"),
                    "--sysroot",
                    str(cwd / "sysroot"),
                    "-c",
                    str(cwd / "src" / "a.c"),
                    "-o",
                    str(cwd / "build" / "a.o"),
                ],
            )

    def test_partial_journals_and_miss_records_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            (cwd / "src").mkdir(parents=True)
            (cwd / "src" / "ok.c").write_text("int ok;\n", encoding="utf-8")
            journal = root / "compile-journal.partial.jsonl"
            journal.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "argv": ["cc", "-c", "src/ok.c", "-o", "ok.o"],
                                "cwd": str(cwd),
                                "src": "src/ok.c",
                                "out": "ok.o",
                            },
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            {
                                "argv": ["cc", "-c", "src/miss.c", "-o", "miss.o"],
                                "cwd": str(cwd),
                                "src": "src/miss.c",
                                "out": "miss.o",
                                "miss": True,
                            },
                            separators=(",", ":"),
                        ),
                        "{",
                    ]
                ),
                encoding="utf-8",
            )

            first = compile_db.emit([journal], root / "compile_commands.json")
            second = compile_db.emit([journal], root / "compile_commands.json")

            self.assertEqual(first.entries, 1)
            self.assertEqual(second.entries, 1)
            data = json.loads((root / "compile_commands.json").read_text(encoding="utf-8"))
            self.assertEqual([entry["file"] for entry in data], [str(cwd / "src" / "ok.c")])

    def test_fallback_generator_runs_when_interposition_journal_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "compile_commands.json"
            generator = root / "write_compile_db.sh"
            generator.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    cat > {str(out)!r} <<'JSON'
                    [{{"directory":{json.dumps(str(root))},"file":{json.dumps(str(root / "generated.c"))},"arguments":["cc","-c","generated.c"]}}]
                    JSON
                    """
                ),
                encoding="utf-8",
            )
            generator.chmod(generator.stat().st_mode | stat.S_IXUSR)

            result = compile_db.emit([], out, fallback=[str(generator)], cwd=root)

            self.assertTrue(result.fallback_used)
            self.assertEqual(result.entries, 1)

    def write_journal(self, path, *entries):
        path.write_text(
            "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()

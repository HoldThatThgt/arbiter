import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from cipher2.cli import main
from cipher2.config import load_config, write_default_config
from cipher2.storage import open_fact_store
from tests.toolchain_helpers import write_fake_toolchain


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(argv, stdout=stdout, stderr=stderr)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class CliInitCommandTest(unittest.TestCase):
    def test_init_empty_repository_writes_config_snapshot_and_json_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, stdout, stderr = _run(["init", str(target), "--json"])
            summary = json.loads(stdout)

            self.assertEqual(exit_code, 0)
            self.assertIn("cipher2 init: sources=0", stderr)
            self.assertIn("cipher2 init: done files=0/0", stderr)
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["command"], "init")
            self.assertIsNotNone(summary["snapshot_id"])
            self.assertEqual(summary["fact_count"], 0)
            self.assertEqual(summary["source_count"], 0)
            self.assertEqual(summary["warning_count"], 0)
            self.assertEqual(summary["setup"]["compile_database"]["action"], "not_found")
            self.assertEqual(summary["setup"]["mcp_config"]["action"], "created")
            self.assertTrue((target / ".cipher" / "config.yml").exists())
            self.assertTrue((target / ".cipher" / "snapshots" / "current").exists())
            self.assertTrue((target / ".mcp.json").exists())

    def test_init_with_source_roots_profile_and_compile_database_writes_expected_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            _write(
                compile_db,
                json.dumps(
                    [
                        {
                            "directory": "..",
                            "file": "src/included.c",
                            "arguments": ["cc", "src/included.c"],
                        }
                    ],
                    sort_keys=True,
                )
                + "\n",
            )
            _write(target / "src" / "included.c", "int included(void) { return 1; }\n")
            _write(target / "src" / "ignored.c", "int ignored(void) { return 2; }\n")
            write_fake_toolchain(target)

            exit_code, stdout, stderr = _run(
                [
                    "init",
                    str(target),
                    "--source-root",
                    "src/included.c",
                    "--profile",
                    "release",
                    "--compile-database",
                    "build/compile_commands.json",
                    "--json",
                ]
            )
            summary = json.loads(stdout)
            facts = list(open_fact_store(target, mode="r", log_enabled=False).iter_facts())

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(summary["source_count"], 1)
            self.assertGreater(summary["fact_count"], 0)
            self.assertEqual(load_config(target, observe=False).compile_database_path, compile_db.resolve(strict=False))
            self.assertTrue(all(fact.object_source.startswith("src/included.c:") for fact in facts))
            self.assertEqual({fact.object_profile for fact in facts}, {"release"})
            self.assertFalse(any("ignored" in fact.object_name for fact in facts))

    def test_init_reports_detected_toolchain_without_writing_toolchain_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "main.c", "int main(void) { return 0; }\n")
            write_fake_toolchain(target)
            (target / ".cipher" / "config.yml").unlink()
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(target / "bin") + os.pathsep + old_path
            try:
                exit_code, stdout, stderr = _run(["init", str(target), "--json"])
            finally:
                os.environ["PATH"] = old_path
            summary = json.loads(stdout)
            config = load_config(target, observe=False)

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(summary["setup"]["toolchain"]["status"], "detected")
            self.assertEqual(summary["setup"]["toolchain"]["backend"], "libclang")
            self.assertEqual(summary["setup"]["toolchain"]["type_driven_ast"], True)
            self.assertEqual(summary["setup"]["toolchain"]["gcc_required"], False)
            self.assertIsNone(config.clang_executable)
            self.assertIsNone(config.gcc_executable)

    def test_existing_config_is_preserved_without_compile_database_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            _write(compile_db, "[]\n")
            write_default_config(target, compile_database="build/compile_commands.json", observe=False)
            before = (target / ".cipher" / "config.yml").read_text(encoding="utf-8")

            exit_code, _stdout, stderr = _run(["init", str(target)])
            after = (target / ".cipher" / "config.yml").read_text(encoding="utf-8")

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(after, before)

    def test_init_auto_discovers_compile_database_and_writes_relative_config_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            _write(compile_db, "[]\n")

            exit_code, stdout, stderr = _run(["init", str(target), "--json"])
            summary = json.loads(stdout)
            config = load_config(target, observe=False)

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(summary["setup"]["compile_database"]["action"], "discovered")
            self.assertEqual(summary["setup"]["compile_database"]["path"], "build/compile_commands.json")
            self.assertEqual(config.compile_database_path, compile_db.resolve(strict=False))
            self.assertIn("  compile_database: build/compile_commands.json\n", (target / ".cipher" / "config.yml").read_text(encoding="utf-8"))

    def test_init_merges_repo_root_mcp_json_and_preserves_other_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            existing = {
                "mcpServers": {"other": {"command": "other-tool", "args": []}},
                "keep": {"nested": True},
            }
            (target / ".mcp.json").write_text(json.dumps(existing, sort_keys=True), encoding="utf-8")

            exit_code, stdout, stderr = _run(["init", str(target), "--json"])
            summary = json.loads(stdout)
            merged = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(summary["setup"]["mcp_config"]["action"], "updated")
            self.assertIn("other", merged["mcpServers"])
            self.assertIn("cipher-2", merged["mcpServers"])
            self.assertEqual(merged["keep"], {"nested": True})
            self.assertEqual(merged["mcpServers"]["cipher-2"]["command"], __import__("sys").executable)
            self.assertIn("serve_stdio", merged["mcpServers"]["cipher-2"]["args"][1])

    def test_init_malformed_mcp_json_warns_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".mcp.json").write_text("{not json", encoding="utf-8")

            exit_code, stdout, stderr = _run(["init", str(target), "--json"])
            summary = json.loads(stdout)

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual((target / ".mcp.json").read_text(encoding="utf-8"), "{not json")
            self.assertEqual(summary["setup"]["mcp_config"]["warning_code"], "mcp_config_malformed")
            self.assertEqual(summary["setup"]["warnings"][1]["code"], "mcp_config_malformed")

    def test_init_no_mcp_config_skips_write_and_print_mcp_config_returns_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, stdout, stderr = _run(["init", str(target), "--json", "--no-mcp-config", "--print-mcp-config"])
            summary = json.loads(stdout)

            self.assertEqual(exit_code, 0, stderr)
            self.assertFalse((target / ".mcp.json").exists())
            self.assertEqual(summary["setup"]["mcp_config"]["action"], "skipped")
            self.assertEqual(summary["setup"]["printed_mcp_config"]["mcpServers"]["cipher-2"]["command"], __import__("sys").executable)


if __name__ == "__main__":
    unittest.main()

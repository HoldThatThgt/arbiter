import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from arbiter_engine.facts.extractor.code import (
    _clear_test_libclang_backend,
    _install_json_test_libclang_backend,
)
from arbiter_engine.facts.store import open_fact_store
from arbiter_engine.shared import pipeline

# Importing the cipher2 test package installs the JSON libclang backend + provides the fake
# toolchain helper, so the build-driven seam extracts hermetically (no real libclang in CI).
from c2.toolchain_helpers import write_fake_toolchain


def _write_journal(path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries),
        encoding="utf-8",
    )


class PipelineSeamTest(unittest.TestCase):
    """The build-driven seam: a green build's compile journal -> compile-db -> CodeFactExtractor
    -> FileFactStore snapshot. (The old per-unit placeholder pipeline + its extract-cache were
    removed when the real cipher-2 extractor was absorbed.)"""

    def setUp(self):
        _install_json_test_libclang_backend()

    def test_green_build_extracts_and_publishes_real_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.c").write_text(
                "int helper(void){return 1;}\nint entry(void){return helper();}\n", encoding="utf-8"
            )
            cdb = root / "compile_commands.json"
            config = write_fake_toolchain(root, compile_database_path=cdb)
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            _write_journal(
                journal,
                {"argv": ["clang", "-c", "src/a.c", "-o", "build/a.o"], "cwd": str(root), "src": "src/a.c", "out": "build/a.o"},
            )

            result = pipeline.publish_after_build(root, [journal], cdb, extractor_config=config)

            self.assertTrue(result.published)
            self.assertIsNotNone(result.snapshot_id)
            self.assertEqual(result.files, 1)
            self.assertEqual(result.warnings, [])
            self.assertGreaterEqual(result.extract_ms, 0)
            self.assertGreaterEqual(result.tail_ms, 0)
            # Read the store while the temp repo still exists (assertions stay inside the block).
            stats = open_fact_store(root, mode="r").stats()
            self.assertGreater(stats.total_facts, 0)
            self.assertEqual(stats.snapshot_id, result.snapshot_id)
            # The snapshot is published where view._base_snapshot_id + the query layer read it.
            self.assertTrue((root / ".arbiter" / "facts" / "snapshots" / "current").exists())

    def test_miss_marker_fails_closed_without_snapshot_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            _write_journal(
                journal,
                {"argv": ["clang", "-c", "src/miss.c", "-o", "build/miss.o"], "cwd": str(root), "src": "src/miss.c", "out": "build/miss.o", "miss": True},
            )

            result = pipeline.publish_after_build(root, [journal], root / "compile_commands.json")

            self.assertFalse(result.published)
            self.assertIsNone(result.snapshot_id)
            self.assertEqual(result.warnings[0]["kind"], "journal_miss")
            self.assertFalse((root / ".arbiter" / "facts" / "snapshots" / "current").exists())

    def test_partial_miss_publishes_clean_tus_with_warning(self):
        # A transient cc fork failure marks ONE TU as miss while the other journaled cleanly.
        # compile_db.emit drops the missed TU, so facts publish from the clean one and the skip
        # is reported as journal_miss_partial — one flaky fork must not nuke the whole snapshot.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.c").write_text(
                "int helper(void){return 1;}\nint entry(void){return helper();}\n", encoding="utf-8"
            )
            cdb = root / "compile_commands.json"
            config = write_fake_toolchain(root, compile_database_path=cdb)
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            _write_journal(
                journal,
                {"argv": ["clang", "-c", "src/a.c", "-o", "build/a.o"], "cwd": str(root), "src": "src/a.c", "out": "build/a.o"},
                {"argv": ["clang", "-c", "src/miss.c", "-o", "build/miss.o"], "cwd": str(root), "src": "src/miss.c", "out": "build/miss.o", "miss": True},
            )

            result = pipeline.publish_after_build(root, [journal], cdb, extractor_config=config)

            self.assertTrue(result.published)
            self.assertIsNotNone(result.snapshot_id)
            self.assertEqual(result.files, 1)
            self.assertIn("journal_miss_partial", [w["kind"] for w in result.warnings])
            self.assertTrue((root / ".arbiter" / "facts" / "snapshots" / "current").exists())

    def test_build_not_green_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            _write_journal(
                journal,
                {"argv": ["clang", "-c", "src/a.c", "-o", "build/a.o"], "cwd": str(root), "src": "src/a.c", "out": "build/a.o"},
            )

            result = pipeline.publish_after_build(
                root, [journal], root / "compile_commands.json", build_succeeded=False
            )

            self.assertFalse(result.published)
            self.assertIsNone(result.snapshot_id)
            self.assertEqual(result.warnings[0]["kind"], "build_failed")

    def test_missing_toolchain_degrades_to_not_published(self):
        # No capable clang -> a typed not-published signal (builds/runs keep working), never a crash.
        _clear_test_libclang_backend()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "src").mkdir()
                (root / "src" / "a.c").write_text("int a(void){return 1;}\n", encoding="utf-8")
                cdb = root / "compile_commands.json"
                config = replace(
                    write_fake_toolchain(root, compile_database_path=cdb),
                    clang_executable=str(root / "bin" / "nonexistent-clang"),
                )
                journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
                _write_journal(
                    journal,
                    {"argv": ["clang", "-c", "src/a.c", "-o", "build/a.o"], "cwd": str(root), "src": "src/a.c", "out": "build/a.o"},
                )

                result = pipeline.publish_after_build(root, [journal], cdb, extractor_config=config)

                self.assertFalse(result.published)
                self.assertIsNone(result.snapshot_id)
                self.assertTrue(result.warnings)
        finally:
            _install_json_test_libclang_backend()

    def test_pool_width_uses_quarter_width_during_build_then_full_width(self):
        self.assertEqual(pipeline.pool_width(16, compiler_active=True), 4)
        self.assertEqual(pipeline.pool_width(16, compiler_active=False), 16)
        self.assertEqual(pipeline.pool_width(3, compiler_active=True), 1)

    def test_pool_width_honors_config_cap(self):
        # facts.index_on_build.pool caps the extraction worker width.
        self.assertEqual(pipeline.pool_width(16, compiler_active=False, cap=4), 4)
        self.assertEqual(pipeline.pool_width(2, compiler_active=False, cap=8), 2)
        self.assertEqual(pipeline.pool_width(8, compiler_active=False, cap=None), 8)
        self.assertEqual(pipeline.pool_width(8, compiler_active=False, cap=0), 8)
        self.assertEqual(pipeline.pool_width(8, compiler_active=False, cap=1), 1)


if __name__ == "__main__":
    unittest.main()

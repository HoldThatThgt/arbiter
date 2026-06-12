import json
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.shared import pipeline


class PipelineTest(unittest.TestCase):
    def test_green_build_drains_and_publishes_snapshot_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "repo"
            (work / "src").mkdir(parents=True)
            source = work / "src" / "a.c"
            source.write_text("int a(void) { return 1; }\n", encoding="utf-8")
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            self.write_journal(
                journal,
                {
                    "argv": ["clang", "-Iinclude", "-O2", "-c", "src/a.c", "-o", "build/a.o"],
                    "cwd": str(work),
                    "src": "src/a.c",
                    "out": "build/a.o",
                },
            )
            extracted = []

            def extractor(unit):
                extracted.append((unit.source, unit.key()))
                return {"warnings": [{"file": unit.source, "message": "stub warning"}]}

            result = pipeline.publish_after_build(
                root,
                [journal],
                root / "compile_commands.json",
                extractor=extractor,
                cpu_count=lambda: 8,
            )

            self.assertTrue(result.published)
            self.assertEqual(result.files, 1)
            self.assertEqual([item[0] for item in extracted], [str(source)])
            self.assertEqual(result.warnings, [{"file": str(source), "message": "stub warning"}])
            self.assertGreaterEqual(result.extract_ms, 0)
            self.assertGreaterEqual(result.tail_ms, 0)
            self.assertGreaterEqual(result.hidden_ms, 0)

            manifest = json.loads(
                (root / ".arbiter" / "facts" / "snapshots" / "current" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["snapshot_id"], result.snapshot_id)
            self.assertEqual(manifest["files"], [str(source)])
            self.assertEqual(manifest["warnings"], result.warnings)

    def test_miss_marker_fails_closed_without_snapshot_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "repo"
            (work / "src").mkdir(parents=True)
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            self.write_journal(
                journal,
                {
                    "argv": ["clang", "-c", "src/miss.c", "-o", "build/miss.o"],
                    "cwd": str(work),
                    "src": "src/miss.c",
                    "out": "build/miss.o",
                    "miss": True,
                },
            )
            calls = []

            result = pipeline.publish_after_build(
                root,
                [journal],
                root / "compile_commands.json",
                extractor=lambda unit: calls.append(unit.source),
            )

            self.assertFalse(result.published)
            self.assertIsNone(result.snapshot_id)
            self.assertEqual(calls, [])
            self.assertEqual(result.files, 0)
            self.assertEqual(result.warnings[0]["kind"], "journal_miss")
            self.assertFalse((root / ".arbiter" / "facts" / "snapshots" / "current").exists())

    def test_header_edit_invalidates_extract_cache_and_snapshot_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "repo"
            (work / "src").mkdir(parents=True)
            (work / "include").mkdir()
            source = work / "src" / "a.c"
            header = work / "include" / "util.h"
            source.write_text('#include "util.h"\nint a(void) { return U; }\n', encoding="utf-8")
            header.write_text("#define U 1\n", encoding="utf-8")
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            self.write_journal(
                journal,
                {
                    "argv": ["clang", "-Iinclude", "-c", "src/a.c", "-o", "build/a.o"],
                    "cwd": str(work),
                    "src": "src/a.c",
                    "out": "build/a.o",
                },
            )
            calls = []

            def extractor(unit):
                calls.append(unit.source)
                return {"warnings": []}

            def publish():
                return pipeline.publish_after_build(
                    root,
                    [journal],
                    root / "compile_commands.json",
                    extractor=extractor,
                    cpu_count=lambda: 2,
                )

            first = publish()
            self.assertEqual(calls, [str(source)])

            unchanged = publish()
            self.assertEqual(calls, [str(source)])
            self.assertEqual(unchanged.snapshot_id, first.snapshot_id)

            header.write_text("#define U 2\n", encoding="utf-8")
            edited = publish()
            self.assertEqual(calls, [str(source), str(source)])
            self.assertNotEqual(edited.snapshot_id, first.snapshot_id)

    def test_failed_extraction_is_not_cached_and_is_retried_next_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "repo"
            (work / "src").mkdir(parents=True)
            source = work / "src" / "a.c"
            source.write_text("int a(void) { return 1; }\n", encoding="utf-8")
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            self.write_journal(
                journal,
                {
                    "argv": ["clang", "-c", "src/a.c", "-o", "build/a.o"],
                    "cwd": str(work),
                    "src": "src/a.c",
                    "out": "build/a.o",
                },
            )

            def failing_extractor(unit):
                raise RuntimeError("boom")

            first = pipeline.publish_after_build(
                root,
                [journal],
                root / "compile_commands.json",
                extractor=failing_extractor,
                cpu_count=lambda: 2,
            )
            self.assertEqual(first.warnings[0]["kind"], "extract_failed")

            cache_path = root / ".arbiter" / "facts" / "extract-cache" / "index.json"
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8")), {})

            retried = []
            second = pipeline.publish_after_build(
                root,
                [journal],
                root / "compile_commands.json",
                extractor=lambda unit: retried.append(unit.source) or {"warnings": []},
                cpu_count=lambda: 2,
            )
            self.assertEqual(retried, [str(source)])
            self.assertEqual(second.warnings, [])
            stored = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored), 1)
            self.assertFalse(next(iter(stored.values()))["failed"])

    def test_store_merges_with_concurrently_written_cache_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "repo"
            (work / "src").mkdir(parents=True)
            source = work / "src" / "a.c"
            source.write_text("int a(void) { return 1; }\n", encoding="utf-8")
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.b1.jsonl"
            self.write_journal(
                journal,
                {
                    "argv": ["clang", "-c", "src/a.c", "-o", "build/a.o"],
                    "cwd": str(work),
                    "src": "src/a.c",
                    "out": "build/a.o",
                },
            )
            cache_path = root / ".arbiter" / "facts" / "extract-cache" / "index.json"

            def extractor(unit):
                # Simulate a concurrent publisher storing its entry between
                # this publish's initial cache load and its store.
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps({"extract:concurrent": {"source": "other/b.c", "failed": False}}),
                    encoding="utf-8",
                )
                return {"warnings": []}

            result = pipeline.publish_after_build(
                root,
                [journal],
                root / "compile_commands.json",
                extractor=extractor,
                cpu_count=lambda: 2,
            )

            self.assertTrue(result.published)
            stored = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("extract:concurrent", stored)
            self.assertEqual(len(stored), 2)

    def test_pool_width_uses_quarter_width_during_build_then_full_width(self):
        self.assertEqual(pipeline.pool_width(16, compiler_active=True), 4)
        self.assertEqual(pipeline.pool_width(16, compiler_active=False), 16)
        self.assertEqual(pipeline.pool_width(3, compiler_active=True), 1)

    def write_journal(self, path, *entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()

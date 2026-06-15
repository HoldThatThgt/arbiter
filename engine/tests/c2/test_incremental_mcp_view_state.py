"""Migrated from cipher-2 tests/test_incremental_mcp_view_state.py (M4 Phase 2, new-native rpc).

Adapted to arbiter's disk-based overlay model: the coordinator publishes the overlay to disk and
any rpc reader merges it (no in-memory fact_view_provider injection). The writer (player) query
reconciles synchronously; a non-writer (executor) reads the published overlay.
"""

import os
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.facts.extractor.code import CodeFactExtractor
from arbiter_engine.facts.incremental import IncrementalBuildResult, IncrementalCoordinator
from arbiter_engine.facts.store import FactRecord, SourceInventoryEntry, open_fact_store

from c2._facts_server import open_facts_server
from c2.incremental_support import load_config
from c2.toolchain_helpers import write_fake_toolchain


def _fact(object_id, source_id, name, description):
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=description,
        object_source="src/alpha.c:1",
        object_profile="debug",
        payload={"fact_kind": "function", "source_id": source_id},
    )


def _source(sha256, *, size_bytes):
    return SourceInventoryEntry(
        source_id="source:a",
        rel_path="src/alpha.c",
        source_kind="c_source",
        sha256=sha256,
        size_bytes=size_bytes,
        mtime_ns=1,
        compile_command_hash="b" * 64,
        toolchain_hash="c" * 64,
        included_by=[],
        includes=[],
    )


class IncrementalMcpViewStateTest(unittest.TestCase):
    def test_search_and_detail_include_base_view_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            fact = _fact("fact:alpha", "source:a", "Alpha", "Alpha function")
            manifest = open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [fact], [], [_source("a" * 64, size_bytes=1)]
            )
            server = open_facts_server(target)

            search = server.call_tool("search", {"query": "alpha", "limit": 10})
            detail = server.call_tool("detail", {"fact_id": "fact:alpha"})

            for result in (search, detail):
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["view_state"], "base")
                self.assertEqual(result.structured_content["base_snapshot_id"], manifest.snapshot_id)
                self.assertIsNone(result.structured_content["overlay_id"])
                self.assertEqual(result.structured_content["stale_source_count"], 0)
                self.assertEqual(result.structured_content["pending_task_count"], 0)
                self.assertIn("snapshot", result.content[0]["text"])

    def test_reader_observes_published_incremental_overlay(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                return IncrementalBuildResult(
                    facts=[_fact("fact:new", "source:a", "New", "new alpha function")],
                    relatives=[],
                    source_inventory=[],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [_fact("fact:old", "source:a", "Old", "old alpha function")],
                [],
                [_source("cba06b5736faf67e54b07b561eae94395e774c517a7d910a54369e1263ccfbd4", size_bytes=3)],
            )
            source_file.write_text("new", encoding="utf-8")
            # The coordinator publishes the overlay to disk; a non-writer rpc query then merges it.
            coordinator = IncrementalCoordinator(target, load_config(target), extractor=FakeExtractor())
            coordinator.notify_file_changed(source_file)

            server = open_facts_server(target, role="QUERY", seat="executor")
            search = server.call_tool("search", {"query": "new", "limit": 10})

            self.assertFalse(search.is_error)
            self.assertEqual(search.structured_content["view_state"], "overlay")
            self.assertIsNotNone(search.structured_content["overlay_id"])
            self.assertEqual(search.structured_content["results"][0]["object_id"], "fact:new")
            self.assertIn("view_state=overlay", search.content[0]["text"])

    def test_writer_reconciles_changed_sources_before_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "main.c"
            source_file.write_text(
                "\n".join(
                    [
                        "int helper(void) { return 1; }",
                        "int caller(void) { return helper(); }",
                        "",
                        "",
                        "int shifted(void) { return 2; }",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = write_fake_toolchain(target)
            result = CodeFactExtractor(target, config).collect([source_file], "default")
            facts = [fact.to_fact_record() for fact in result.facts]
            shifted_id = next(fact.object_id for fact in facts if fact.object_name == "shifted")
            source_mtime_ns = next(entry.mtime_ns for entry in result.source_inventory if entry.rel_path == "src/main.c")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                facts, result.relatives, result.source_inventory
            )
            source_file.write_text(
                "\n".join(
                    [
                        "int helper(void) { return 1; }",
                        "int caller(void) { return 0; }",
                        "int shifted(void) { return 2; }",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(source_file, ns=(source_mtime_ns + 1_000_000_000, source_mtime_ns + 1_000_000_000))

            # The fake clang must be discoverable for the writer's reconcile (version probe).
            server = open_facts_server(target, extra_path=str(target / "bin"))
            callees = server.call_tool("search", {"query": "callees:caller", "limit": 10})
            detail = server.call_tool("detail", {"fact_id": shifted_id, "budget": "small"})

            self.assertFalse(callees.is_error)
            self.assertEqual(callees.structured_content["view_state"], "overlay")
            self.assertIsNotNone(callees.structured_content["overlay_id"])
            self.assertEqual(callees.structured_content["result_count"], 0)
            self.assertEqual(callees.structured_content["results"], [])
            self.assertFalse(detail.is_error)
            self.assertEqual(detail.structured_content["view_state"], "overlay")
            self.assertEqual(detail.structured_content["fact"]["object_source"], "src/main.c:3")
            self.assertEqual(detail.structured_content["source_context"]["unavailable_reason"], None)
            self.assertIn("int shifted(void) { return 2; }", detail.structured_content["source_context"]["lines"])


if __name__ == "__main__":
    unittest.main()

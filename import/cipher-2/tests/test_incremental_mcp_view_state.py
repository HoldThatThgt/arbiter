import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from cipher2.config import load_config
from cipher2.incremental import IncrementalBuildResult, IncrementalCoordinator
from cipher2.initializer.extractor.code import CodeFactExtractor
from cipher2.mcp import open_mcp_server
from cipher2.storage import FactRecord, SourceInventoryEntry, TemporaryOverlay, open_fact_store
from tests.toolchain_helpers import write_fake_toolchain


class IncrementalMcpViewStateTest(unittest.TestCase):
    def test_search_and_detail_include_base_view_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            (target / "src" / "alpha.c").write_text("x", encoding="utf-8")
            fact = FactRecord(
                object_id="fact:alpha",
                object_name="Alpha",
                object_description="Alpha function",
                object_source="src/alpha.c:1",
                object_profile="debug",
                payload={"fact_kind": "function", "source_id": "source:a"},
            )
            source = SourceInventoryEntry(
                source_id="source:a",
                rel_path="src/alpha.c",
                source_kind="c_source",
                sha256=hashlib.sha256(b"x").hexdigest(),
                size_bytes=1,
                mtime_ns=1,
                compile_command_hash="b" * 64,
                toolchain_hash="c" * 64,
                included_by=[],
                includes=[],
            )
            manifest = open_fact_store(target, mode="w", log_enabled=False).replace_snapshot([fact], [], [source])
            server = open_mcp_server(target, log_enabled=True)

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

    def test_search_includes_pending_base_view_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            fact = FactRecord(
                object_id="fact:alpha",
                object_name="Alpha",
                object_description="Alpha function",
                object_source="src/alpha.c:1",
                object_profile="debug",
                payload={"fact_kind": "function", "source_id": "source:a"},
            )
            source = SourceInventoryEntry(
                source_id="source:a",
                rel_path="src/alpha.c",
                source_kind="c_source",
                sha256="a" * 64,
                size_bytes=1,
                mtime_ns=1,
                compile_command_hash="b" * 64,
                toolchain_hash="c" * 64,
                included_by=[],
                includes=[],
            )
            store = open_fact_store(target, mode="w", log_enabled=False)
            store.replace_snapshot([fact], [], [source])
            view = store.open_view(
                TemporaryOverlay(
                    overlay_id="pending-1",
                    view_state="pending",
                    stale_source_count=1,
                    pending_task_count=1,
                )
            )
            server = open_mcp_server(target, fact_view_provider=lambda: view)

            search = server.call_tool("search", {"query": "alpha", "limit": 10})

            self.assertFalse(search.is_error)
            self.assertEqual(search.structured_content["view_state"], "pending")
            self.assertEqual(search.structured_content["stale_source_count"], 1)
            self.assertEqual(search.structured_content["pending_task_count"], 1)
            self.assertEqual(search.structured_content["results"][0]["object_id"], "fact:alpha")

    def test_mcp_reads_injected_incremental_overlay_view(self):
        class FakeExtractor:
            def extract_dirty_sources(self, dirty_sources, profile):
                return IncrementalBuildResult(
                    facts=[
                        FactRecord(
                            object_id="fact:new",
                            object_name="New",
                            object_description="new alpha function",
                            object_source="src/alpha.c:1",
                            object_profile="debug",
                            payload={"fact_kind": "function", "source_id": "source:a"},
                        )
                    ],
                    relatives=[],
                    source_inventory=[],
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "src").mkdir()
            source_file = target / "src" / "alpha.c"
            source_file.write_text("old", encoding="utf-8")
            source = SourceInventoryEntry(
                source_id="source:a",
                rel_path="src/alpha.c",
                source_kind="c_source",
                sha256="cba06b5736faf67e54b07b561eae94395e774c517a7d910a54369e1263ccfbd4",
                size_bytes=3,
                mtime_ns=1,
                compile_command_hash="b" * 64,
                toolchain_hash="c" * 64,
                included_by=[],
                includes=[],
            )
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                [
                    FactRecord(
                        object_id="fact:old",
                        object_name="Old",
                        object_description="old alpha function",
                        object_source="src/alpha.c:1",
                        object_profile="debug",
                        payload={"fact_kind": "function", "source_id": "source:a"},
                    )
                ],
                [],
                [source],
            )
            source_file.write_text("new", encoding="utf-8")
            coordinator = IncrementalCoordinator(target, load_config(target, observe=False), extractor=FakeExtractor())
            coordinator.notify_file_changed(source_file)
            server = open_mcp_server(target, fact_view_provider=coordinator.current_view)

            search = server.call_tool("search", {"query": "new", "limit": 10})

            self.assertFalse(search.is_error)
            self.assertEqual(search.structured_content["view_state"], "overlay")
            self.assertIsNotNone(search.structured_content["overlay_id"])
            self.assertEqual(search.structured_content["results"][0]["object_id"], "fact:new")
            self.assertIn("view_state=overlay", search.content[0]["text"])

    def test_open_mcp_server_reconciles_changed_sources_before_queries(self):
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
            write_fake_toolchain(target)
            config = load_config(target, observe=False)
            result = CodeFactExtractor(target, config).collect([source_file], "default")
            facts = [fact.to_fact_record() for fact in result.facts]
            shifted_id = next(fact.object_id for fact in facts if fact.object_name == "shifted")
            source_mtime_ns = next(entry.mtime_ns for entry in result.source_inventory if entry.rel_path == "src/main.c")
            open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
                facts,
                result.relatives,
                result.source_inventory,
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

            server = open_mcp_server(target, log_enabled=False)
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

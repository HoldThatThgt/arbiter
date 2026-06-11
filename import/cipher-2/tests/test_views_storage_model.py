import json
import os
import tempfile
import unittest
from pathlib import Path

from cipher2.storage import FactRecord, open_fact_store
from cipher2.tools.views import StorageViewModel, build_overview


def _fact(index: int):
    return FactRecord(
        object_id=f"fact:{index}",
        object_name=f"Fact {index}",
        object_description="Storage view input",
        object_source="src/view.py:1" if index % 2 else "coverage:lrun",
        object_profile="debug" if index % 2 else "release",
        object_caller="caller" if index % 2 else None,
        object_callee="callee" if index % 3 == 0 else None,
        payload={"fact_kind": "function" if index % 2 else "coverage"},
    )


class ViewsStorageModelTest(unittest.TestCase):
    def test_empty_storage_model_is_empty_without_creating_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            overview = build_overview(target, include_sections=["storage"])

            self.assertEqual(overview.state, "empty")
            self.assertIsInstance(overview.storage, StorageViewModel)
            self.assertEqual(overview.storage.state, "empty")
            self.assertEqual(overview.storage.total_facts, 0)
            self.assertEqual(overview.storage.snapshot_format, None)
            self.assertEqual(overview.storage.compression, None)
            self.assertEqual(overview.storage.bytes_on_disk, 0)
            self.assertEqual(overview.storage.uncompressed_bytes, 0)
            self.assertEqual(overview.storage.compression_ratio, 1.0)
            self.assertEqual(overview.storage.storage_overhead_ratio, 1.0)
            self.assertEqual(overview.storage.file_bytes, {})
            self.assertEqual(overview.storage.read_index_state, "missing")
            self.assertEqual(overview.storage.read_index_bytes, 0)
            self.assertEqual(overview.storage.read_index_schema_version, None)
            self.assertEqual(overview.storage.read_index_codec, None)
            self.assertEqual(overview.storage.fact_kinds, {})
            self.assertEqual(overview.storage.search_count, 0)
            self.assertEqual(overview.storage.error_count, 0)
            self.assertFalse((target / ".cipher" / "snapshots").exists())

    def test_ready_storage_model_combines_stats_and_storage_log_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w")
            store.replace_facts([_fact(1), _fact(2), _fact(3)])
            store.search("fact", limit=2)

            overview = build_overview(target, include_sections=["storage"], top_n=1)

            self.assertEqual(overview.state, "ready")
            self.assertEqual(overview.storage.state, "ready")
            self.assertEqual(overview.storage.total_facts, 3)
            self.assertEqual(overview.storage.snapshot_format, "compact-jsonl-gzip")
            self.assertEqual(overview.storage.compression, "gzip-1")
            self.assertGreater(overview.storage.bytes_on_disk, 0)
            self.assertGreater(overview.storage.uncompressed_bytes, 0)
            self.assertGreater(overview.storage.compression_ratio, 0)
            self.assertGreater(overview.storage.storage_overhead_ratio, 0)
            self.assertEqual(overview.storage.read_index_state, "ready")
            self.assertGreater(overview.storage.read_index_bytes, 0)
            self.assertEqual(overview.storage.read_index_schema_version, 6)
            self.assertEqual(overview.storage.read_index_codec, "json-text")
            self.assertEqual(set(overview.storage.file_bytes), {"facts", "relatives", "source_inventory"})
            self.assertEqual(overview.storage.fact_kinds, {"function": 2})
            self.assertEqual(overview.storage.profiles, {"debug": 2})
            self.assertEqual(overview.storage.source_files, {"src/view.py": 2})
            self.assertEqual(overview.storage.snapshot_count, 1)
            self.assertEqual(overview.storage.lock_state, "free")
            self.assertEqual(overview.storage.search_count, 1)
            self.assertEqual(overview.storage.error_count, 0)

    def test_log_degraded_storage_model_is_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "log").write_text("not a directory", encoding="utf-8")
            open_fact_store(target, mode="w").replace_facts([_fact(1)])

            overview = build_overview(target, include_sections=["storage"])

            self.assertEqual(overview.state, "warning")
            self.assertEqual(overview.storage.state, "warning")
            self.assertEqual(overview.storage.log_write_failures, 1)
            self.assertEqual(overview.storage.latest_log_error_code, "log_write_failed")

    def test_stale_lock_storage_model_is_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            lock_dir = target / ".cipher" / "run" / "storage.lock"
            lock_dir.mkdir(parents=True)
            (lock_dir / "owner.json").write_text(json.dumps({"pid": 99999999}), encoding="utf-8")

            overview = build_overview(target, include_sections=["storage"])

            self.assertEqual(overview.state, "warning")
            self.assertEqual(overview.storage.state, "warning")
            self.assertEqual(overview.storage.lock_state, "stale_likely")

    def test_storage_stats_failure_is_section_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact(1)])
            snapshot_id = (target / ".cipher" / "snapshots" / "current").read_text(encoding="utf-8")
            (target / ".cipher" / "snapshots" / snapshot_id / "stats.json").write_text("{}", encoding="utf-8")

            overview = build_overview(target, include_sections=["storage"])

            self.assertEqual(overview.state, "error")
            self.assertIsNone(overview.storage)
            self.assertEqual([(error.section, error.code) for error in overview.errors], [("storage", "storage_unreadable")])
            self.assertNotIn("Traceback", overview.errors[0].message)


if __name__ == "__main__":
    unittest.main()

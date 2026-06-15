# Migrated from cipher-2 tests/test_storage_view_model.py (M4 acceptance — imports rewritten cipher2.*->arbiter_engine.facts.*, .cipher->.arbiter/facts).
import json
import os
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.facts.store import FactRecord, StorageError, open_fact_store


def _fact(index: int):
    return FactRecord(
        object_id=f"fact:{index}",
        object_name=f"Fact {index}",
        object_description="View model input",
        object_source="src/view.py:1" if index % 2 else "coverage:lrun",
        object_profile="debug" if index % 2 else "release",
        object_caller="caller" if index % 2 else None,
        object_callee="callee" if index % 3 == 0 else None,
        payload={"fact_kind": "function" if index % 2 else "coverage"},
    )


def _view_state_from_stats(stats):
    if stats.total_facts == 0:
        return "empty"
    if stats.log_write_failures:
        return "warning"
    if stats.lock_state in {"held", "stale_likely"}:
        return "warning"
    return "ready"


class StorageViewModelInputTest(unittest.TestCase):
    def test_empty_stats_support_empty_view_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = open_fact_store(Path(tmp), mode="r", log_enabled=False).stats()

            self.assertEqual(_view_state_from_stats(stats), "empty")
            self.assertEqual(stats.total_facts, 0)
            self.assertEqual(stats.snapshot_format, None)
            self.assertEqual(stats.compression, None)
            self.assertEqual(stats.bytes_on_disk, 0)
            self.assertEqual(stats.uncompressed_bytes, 0)
            self.assertEqual(stats.compression_ratio, 1.0)
            self.assertEqual(stats.storage_overhead_ratio, 1.0)
            self.assertEqual(stats.file_bytes, {})
            self.assertEqual(stats.read_index_state, "missing")
            self.assertEqual(stats.read_index_bytes, 0)
            self.assertEqual(stats.read_index_schema_version, None)
            self.assertEqual(stats.read_index_codec, None)
            self.assertEqual(stats.fact_kinds, {})
            self.assertEqual(stats.profiles, {})
            self.assertEqual(stats.source_files, {})

    def test_ready_stats_expose_core_view_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            store.replace_facts([_fact(1), _fact(2), _fact(3)])
            stats = store.stats()

            self.assertEqual(_view_state_from_stats(stats), "ready")
            self.assertEqual(stats.total_facts, 3)
            self.assertEqual(stats.snapshot_format, "compact-jsonl-gzip")
            self.assertEqual(stats.compression, "gzip-1")
            self.assertGreater(stats.bytes_on_disk, 0)
            self.assertGreater(stats.uncompressed_bytes, 0)
            self.assertGreater(stats.compression_ratio, 0)
            self.assertGreater(stats.storage_overhead_ratio, 0)
            self.assertEqual(stats.read_index_state, "ready")
            self.assertGreater(stats.read_index_bytes, 0)
            self.assertEqual(stats.read_index_schema_version, 6)
            self.assertEqual(stats.read_index_codec, "json-text")
            self.assertEqual(set(stats.file_bytes), {"facts", "relatives", "source_inventory"})
            self.assertEqual(stats.fact_kinds, {"coverage": 1, "function": 2})
            self.assertEqual(stats.profiles, {"debug": 2, "release": 1})
            self.assertEqual(stats.source_files, {"coverage": 1, "src/view.py": 2})
            self.assertEqual(stats.with_caller_count, 2)
            self.assertEqual(stats.with_callee_count, 1)
            self.assertGreater(stats.bytes_on_disk_total, 0)

    @unittest.skip(
        "arbiter runs the fact store log-disabled (forensics live in the referee journal, "
        "not cipher-2's tools.log); log-write-failure accounting is intentionally not ported"
    )
    def test_log_degraded_stats_support_warning_view_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".arbiter" / "facts").mkdir(parents=True)
            (target / ".arbiter" / "facts" / "log").write_text("not a directory", encoding="utf-8")
            open_fact_store(target, mode="w").replace_facts([_fact(1)])

            stats = open_fact_store(target, mode="r").stats()

            self.assertEqual(_view_state_from_stats(stats), "warning")
            self.assertEqual(stats.log_write_failures, 1)
            self.assertEqual(stats.latest_log_error_code, "log_write_failed")

    def test_lock_state_supports_lock_warning_view_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            lock_dir = target / ".arbiter" / "facts" / "run" / "storage.lock"
            lock_dir.mkdir(parents=True)
            (lock_dir / "owner.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

            stats = open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(stats.lock_state, "held")
            self.assertEqual(_view_state_from_stats(stats), "empty")

    def test_corruption_error_is_structured_for_error_view_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact(1)])
            snapshot_id = (target / ".arbiter" / "facts" / "snapshots" / "current").read_text(encoding="utf-8")
            (target / ".arbiter" / "facts" / "snapshots" / snapshot_id / "stats.json").write_text("{}", encoding="utf-8")

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(caught.exception.code, "stats_mismatch")
            self.assertNotIn("Traceback", caught.exception.message)


if __name__ == "__main__":
    unittest.main()

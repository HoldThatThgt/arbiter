# Migrated from cipher-2 tests/test_storage_source_inventory.py (M4 acceptance — imports rewritten cipher2.*->arbiter_engine.facts.*, .cipher->.arbiter/facts).
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.facts.store import FactRecord, SourceInventoryEntry, StorageError, open_fact_store


def _fact(object_id: str, source_id: str, name: str = "Alpha"):
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=f"{name} function",
        object_source="src/alpha.c:1",
        object_profile="debug",
        payload={"fact_kind": "function", "source_id": source_id},
    )


def _source(source_id: str, rel_path: str = "src/alpha.c", **overrides):
    data = {
        "source_id": source_id,
        "rel_path": rel_path,
        "source_kind": "c_source",
        "sha256": "a" * 64,
        "size_bytes": 12,
        "mtime_ns": 100,
        "compile_command_hash": "b" * 64,
        "toolchain_hash": "c" * 64,
        "included_by": [],
        "includes": [],
    }
    data.update(overrides)
    return SourceInventoryEntry(**data)


class StorageSourceInventoryTest(unittest.TestCase):
    def test_replace_snapshot_writes_v5_gzip_source_inventory_index_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)

            manifest = store.replace_snapshot([_fact("fact:a", "source:a")], [], [_source("source:a")])

            snapshot_dir = target / ".arbiter" / "facts" / "snapshots" / manifest.snapshot_id
            self.assertEqual(manifest.schema_version, 5)
            self.assertEqual(manifest.source_count, 1)
            self.assertEqual(manifest.stats["total_sources"], 1)
            self.assertTrue((snapshot_dir / "source_inventory.jsonl.gz").exists())
            self.assertTrue((snapshot_dir / "read_index.sqlite").exists())
            self.assertFalse((snapshot_dir / "source_inventory.jsonl").exists())
            rows = [
                json.loads(line)
                for line in gzip.open(snapshot_dir / "source_inventory.jsonl.gz", "rt", encoding="utf-8")
                if line.strip()
            ]
            self.assertEqual(rows[0]["schema_version"], 5)
            self.assertEqual(rows[0]["source_id"], "source:a")
            self.assertEqual([item.source_id for item in store.iter_source_inventory()], ["source:a"])
            self.assertEqual(store.stats().total_sources, 1)

    def test_source_inventory_rejects_path_escape_and_bad_hash(self):
        with self.assertRaises(StorageError) as bad_path:
            _source("source:bad", rel_path="../outside.c")
        self.assertEqual(bad_path.exception.code, "path_escape")

        with self.assertRaises(StorageError) as bad_hash:
            _source("source:bad", sha256="short")
        self.assertEqual(bad_hash.exception.code, "invalid_source_inventory")


if __name__ == "__main__":
    unittest.main()

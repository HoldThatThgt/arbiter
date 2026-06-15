# Migrated from cipher-2 tests/test_storage_relative_no_compat.py (M4 acceptance — imports rewritten cipher2.*->arbiter_engine.facts.*, .cipher->.arbiter/facts).
import json
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.facts.store import FactRecord, StorageError, open_fact_store


class StorageRelativeNoCompatTest(unittest.TestCase):
    def test_old_schema_v1_snapshot_is_not_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            snapshot_id = "sha256-old"
            snapshot_dir = target / ".arbiter" / "facts" / "snapshots" / snapshot_id
            snapshot_dir.mkdir(parents=True)
            (target / ".arbiter" / "facts" / "snapshots" / "current").write_text(snapshot_id, encoding="utf-8")
            (snapshot_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "snapshot_id": snapshot_id,
                        "reused": False,
                        "created_at": "2026-05-26T00:00:00.000000Z",
                        "fact_count": 0,
                        "facts_sha256": "0" * 64,
                        "bytes_on_disk": 0,
                        "stats": {},
                        "log_write_failures": 0,
                        "latest_log_error_code": None,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            (snapshot_dir / "stats.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(caught.exception.code, "unsupported_schema_version")

    def test_missing_relatives_file_is_manifest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)
            manifest = store.replace_snapshot(
                [
                    FactRecord(
                        object_id="fact:file:a",
                        object_name="a",
                        object_description="file",
                        object_source="src/a.c:1",
                        object_profile="debug",
                        payload={"fact_kind": "code_file"},
                    )
                ],
                [],
            )
            (target / ".arbiter" / "facts" / "snapshots" / manifest.snapshot_id / "relatives.jsonl.gz").unlink()

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(caught.exception.code, "manifest_mismatch")


if __name__ == "__main__":
    unittest.main()

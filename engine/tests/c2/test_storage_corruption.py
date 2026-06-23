# Migrated from cipher-2 tests/test_storage_corruption.py (M4 acceptance — imports rewritten cipher2.*->arbiter_engine.facts.*, .cipher->.arbiter/facts).
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.facts.store import FactRecord, StorageError, open_fact_store


def _fact():
    return FactRecord(
        object_id="fact:one",
        object_name="One",
        object_description="One fact",
        object_source="src/one.py:1",
        object_profile="debug",
        payload={"fact_kind": "function"},
    )


def _snapshot_dir(target: Path) -> Path:
    snapshot_id = (target / ".arbiter" / "facts" / "snapshots" / "current").read_text(encoding="utf-8")
    return target / ".arbiter" / "facts" / "snapshots" / snapshot_id


class StorageCorruptionTest(unittest.TestCase):
    def test_malformed_gzip_facts_jsonl_is_snapshot_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            (_snapshot_dir(target) / "facts.jsonl.gz").write_bytes(b"not gzip\n")

            with self.assertRaises(StorageError) as caught:
                list(open_fact_store(target, mode="r", log_enabled=False).iter_facts())

            self.assertEqual(caught.exception.code, "snapshot_corrupt")

    def test_facts_hash_mismatch_is_manifest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            path = _snapshot_dir(target) / "facts.jsonl.gz"
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                content = handle.read()
            with gzip.open(path, "wt", encoding="utf-8", compresslevel=1) as handle:
                handle.write(content + "\n")

            with self.assertRaises(StorageError) as caught:
                list(open_fact_store(target, mode="r", log_enabled=False).iter_facts())

            self.assertEqual(caught.exception.code, "manifest_mismatch")

    def test_cached_search_revalidates_modified_facts_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            store = open_fact_store(target, mode="r", log_enabled=False)
            self.assertEqual([fact.object_id for fact in store.search("One", limit=1)], ["fact:one"])
            path = _snapshot_dir(target) / "facts.jsonl.gz"
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                content = handle.read()
            with gzip.open(path, "wt", encoding="utf-8", compresslevel=1) as handle:
                handle.write(content + "\n")

            with self.assertRaises(StorageError) as caught:
                store.search("One", limit=1)

            self.assertEqual(caught.exception.code, "manifest_mismatch")

    def test_v3_plaintext_snapshot_is_not_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            snapshot_id = "sha256-v3"
            snapshot_dir = target / ".arbiter" / "facts" / "snapshots" / snapshot_id
            snapshot_dir.mkdir(parents=True)
            (target / ".arbiter" / "facts" / "snapshots" / "current").write_text(snapshot_id, encoding="utf-8")
            stats = {"bytes_on_disk": 0}
            manifest = {
                "schema_version": 3,
                "snapshot_id": snapshot_id,
                "reused": False,
                "created_at": "2026-05-28T00:00:00.000000Z",
                "fact_count": 0,
                "relative_count": 0,
                "source_count": 0,
                "facts_sha256": "0" * 64,
                "relatives_sha256": "0" * 64,
                "source_inventory_sha256": "0" * 64,
                "bytes_on_disk": 0,
                "stats": stats,
                "log_write_failures": 0,
                "latest_log_error_code": None,
            }
            (snapshot_dir / "facts.jsonl").write_text("", encoding="utf-8")
            (snapshot_dir / "relatives.jsonl").write_text("", encoding="utf-8")
            (snapshot_dir / "source_inventory.jsonl").write_text("", encoding="utf-8")
            (snapshot_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            (snapshot_dir / "stats.json").write_text(json.dumps(stats, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(caught.exception.code, "unsupported_schema_version")

    def test_unsupported_schema_version_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            manifest_path = _snapshot_dir(target) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 99
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(caught.exception.code, "unsupported_schema_version")

    def test_current_pointer_to_missing_snapshot_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            current = target / ".arbiter" / "facts" / "snapshots" / "current"
            current.parent.mkdir(parents=True)
            current.write_text("sha256-missing", encoding="utf-8")

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(caught.exception.code, "missing_snapshot")

    def test_stats_mismatch_manifest_stats_differs_from_stats_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            stats_path = _snapshot_dir(target) / "stats.json"
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            stats["total_facts"] = 99
            stats_path.write_text(json.dumps(stats, sort_keys=True), encoding="utf-8")

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(caught.exception.code, "stats_mismatch")

    def test_stats_mismatch_manifest_bytes_differs_from_stats_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            manifest_path = _snapshot_dir(target) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["bytes_on_disk"] = manifest["stats"]["bytes_on_disk"] + 1
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(caught.exception.code, "stats_mismatch")

    def test_stats_mismatch_actual_disk_bytes_differs_from_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            (_snapshot_dir(target) / "stats.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).stats()

            self.assertEqual(caught.exception.code, "stats_mismatch")

    def test_missing_persistent_read_index_is_manifest_mismatch_for_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            (_snapshot_dir(target) / "read_index.sqlite").unlink()

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).search("one", limit=1)

            self.assertEqual(caught.exception.code, "manifest_mismatch")

    def test_old_read_index_schema_version_is_manifest_mismatch_for_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            read_index = _snapshot_dir(target) / "read_index.sqlite"
            with sqlite3.connect(read_index) as connection:
                connection.execute(
                    "UPDATE index_metadata SET value = ? WHERE key = 'schema_version'",
                    ("5",),
                )

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).search("one", limit=1)

            self.assertEqual(caught.exception.code, "manifest_mismatch")

    def test_corrupt_persistent_read_index_is_snapshot_corrupt_for_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            read_index = _snapshot_dir(target) / "read_index.sqlite"
            read_index.write_bytes(b"\0" * read_index.stat().st_size)

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="r", log_enabled=False).search("one", limit=1)

            self.assertEqual(caught.exception.code, "snapshot_corrupt")

    def test_publish_over_manifestless_snapshot_dir_does_not_raise_raw_oserror(self):
        # A snapshot dir that exists but has no manifest (interrupted legacy
        # write / external tampering / partial delete) must not wedge future
        # publishes of the same content with a raw OSError [Errno 66]; the
        # publish should clear the stale dir and republish cleanly.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            snapshot_dir = _snapshot_dir(target)
            # Drop the manifest/stats but keep other (non-empty) files so the dir
            # is manifest-less yet non-empty -- the os.replace [Errno 66] case.
            (snapshot_dir / "manifest.json").unlink()
            (snapshot_dir / "stats.json").unlink()
            self.assertTrue(any(snapshot_dir.iterdir()))

            # Republishing the identical facts hits the not-reused path (no
            # manifest to reuse) and must succeed rather than raise OSError.
            try:
                manifest = open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            except OSError as exc:  # pragma: no cover - regression guard
                self.fail(f"publish raised raw OSError instead of staying typed: {exc!r}")
            self.assertFalse(manifest.reused)
            self.assertTrue((snapshot_dir / "manifest.json").exists())
            self.assertEqual(
                [fact.object_id for fact in open_fact_store(target, mode="r", log_enabled=False).iter_facts()],
                ["fact:one"],
            )

    def test_current_pointer_with_trailing_whitespace_still_resolves(self):
        # The reader must tolerate a `current` pointer carrying trailing
        # whitespace (manual edit / alternate writer), matching the view layer.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            current = target / ".arbiter" / "facts" / "snapshots" / "current"
            current.write_text(current.read_text(encoding="utf-8").strip() + "\n  \n", encoding="utf-8")

            store = open_fact_store(target, mode="r", log_enabled=False)
            self.assertEqual([fact.object_id for fact in store.iter_facts()], ["fact:one"])
            self.assertEqual(store.stats().total_facts, 1)


if __name__ == "__main__":
    unittest.main()

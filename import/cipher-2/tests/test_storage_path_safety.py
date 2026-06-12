import json
import os
import tempfile
import unittest
from pathlib import Path

from cipher2.storage import FactRecord, StorageError, open_fact_store
from cipher2.storage.recovery import force_unlock


def _fact():
    return FactRecord(
        object_id="fact:one",
        object_name="One",
        object_description="One fact",
        object_source="src/one.py:1",
        object_profile="debug",
        payload={"fact_kind": "function"},
    )


class StoragePathSafetyTest(unittest.TestCase):
    def test_write_outputs_stay_inside_target_cipher_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            manifest = open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])

            snapshot_dir = target / ".cipher" / "snapshots" / manifest.snapshot_id
            for path in snapshot_dir.iterdir():
                self.assertTrue(path.is_relative_to(target / ".cipher"))
            self.assertFalse((Path.cwd() / ".cipher").exists())

    def test_read_only_open_does_not_create_runtime_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="r", log_enabled=False)

            self.assertEqual(store.stats().total_facts, 0)
            self.assertFalse((target / ".cipher").exists())

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            os.symlink(Path(outside), target / ".cipher" / "snapshots")

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])

            self.assertEqual(caught.exception.code, "path_escape")

    def test_lock_busy_is_rejected_and_force_unlock_removes_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            lock_dir = target / ".cipher" / "run" / "storage.lock"
            lock_dir.mkdir(parents=True)
            (lock_dir / "owner.json").write_text(
                json.dumps({"pid": 99999999, "host": "localhost", "created_at": "2026-05-25T10:00:00.000000Z"}),
                encoding="utf-8",
            )

            with self.assertRaises(StorageError) as caught:
                open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            self.assertEqual(caught.exception.code, "lock_busy")
            self.assertEqual(open_fact_store(target, mode="r", log_enabled=False).stats().lock_state, "stale_likely")

            self.assertTrue(force_unlock(target))
            self.assertFalse(lock_dir.exists())

    def test_force_unlock_keeps_live_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            lock_dir = target / ".cipher" / "run" / "storage.lock"
            lock_dir.mkdir(parents=True)
            (lock_dir / "owner.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

            self.assertFalse(force_unlock(target))
            self.assertTrue(lock_dir.exists())


if __name__ == "__main__":
    unittest.main()

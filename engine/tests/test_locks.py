import os
import tempfile
import time
import unittest
from pathlib import Path

from arbiter_engine import errors
from arbiter_engine.shared import locks


class LockInventoryTest(unittest.TestCase):
    def test_inventory_paths_are_repo_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            self.assertEqual(
                locks.path_for(root, locks.MATCH),
                root / ".arbiter" / "locks" / "match.lock",
            )
            self.assertEqual(
                locks.path_for(root, locks.SNAPSHOT),
                root / ".arbiter" / "locks" / "snapshot.lock",
            )
            build = locks.build_lock(root)
            self.assertEqual(build.name, "build")
            self.assertEqual(build.label, f"build/{build.key}.lock")
            self.assertEqual(
                locks.path_for(root, build),
                root / ".arbiter" / "locks" / "build" / f"{build.key}.lock",
            )

    def test_order_violation_is_asserted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaises(AssertionError):
                with locks.acquire(root, [locks.STATE, locks.SNAPSHOT], timeout_s=0.05):
                    pass

    def test_timeout_raises_typed_lock_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with locks.acquire(root, [locks.STATE], timeout_s=0.2):
                start = time.monotonic()
                with self.assertRaises(errors.RPCError) as ctx:
                    with locks.acquire(root, [locks.STATE], timeout_s=0.05):
                        pass

            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(ctx.exception.data, {"kind": "lock_timeout", "lock": "state.lock"})

    def test_no_ad_hoc_engine_flocks(self):
        engine_root = Path(__file__).resolve().parents[1] / "arbiter_engine"
        offenders = []
        for path in engine_root.rglob("*.py"):
            rel = path.relative_to(engine_root).as_posix()
            if rel == "shared/locks.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "fcntl.flock" in text or "LOCK_EX" in text or "LOCK_NB" in text:
                offenders.append(rel)

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

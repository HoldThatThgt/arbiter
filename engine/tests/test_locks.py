import errno
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_non_contention_flock_error_closes_handle(self):
        # A non-BlockingIOError flock failure (ENOLCK etc.) must not leak the just-opened
        # lock-file descriptor: acquire never appends it to `held`, so the finally cleanup
        # cannot see it — the open/flock path itself has to close it before re-raising.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            opened = []
            real_open = Path.open

            def tracking_open(self, *args, **kwargs):
                handle = real_open(self, *args, **kwargs)
                opened.append(handle)
                return handle

            def failing_flock(fd, op):
                raise OSError(errno.ENOLCK, "no locks available")

            with mock.patch.object(Path, "open", tracking_open), mock.patch.object(
                locks.fcntl, "flock", failing_flock
            ):
                with self.assertRaises(OSError) as ctx:
                    with locks.acquire(root, [locks.STATE], timeout_s=0.2):
                        pass

            self.assertEqual(ctx.exception.errno, errno.ENOLCK)
            self.assertTrue(opened, "acquire should have opened the lock file")
            for handle in opened:
                self.assertTrue(handle.closed, "leaked an open lock-file handle on flock error")

    def test_no_ad_hoc_engine_flocks(self):
        engine_dir = Path(__file__).resolve().parents[1]
        # shared/locks.py is the single sanctioned flock home; the contention
        # test embeds a fixture worker that uses raw flock to observe
        # serialization from outside the engine, and this meta-test names the
        # tokens it scans for.
        allowlist = {
            "arbiter_engine/shared/locks.py",
            "tests/test_locks.py",
            "tests/test_runner_contention.py",
        }
        offenders = []
        for root in ("arbiter_engine", "tests"):
            for path in (engine_dir / root).rglob("*.py"):
                rel = path.relative_to(engine_dir).as_posix()
                if rel in allowlist:
                    continue
                text = path.read_text(encoding="utf-8")
                if "fcntl.flock" in text or "LOCK_EX" in text or "LOCK_NB" in text:
                    offenders.append(rel)

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.shared import census


class CensusTest(unittest.TestCase):
    def test_detects_new_deleted_changed_and_ignores_touch_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            a = root / "src" / "a.c"
            b = root / "src" / "b.c"
            a.write_text("int a;\n", encoding="utf-8")
            b.write_text("int b;\n", encoding="utf-8")

            first = census.scan(root, ["src/**/*.c"])
            self.assertEqual(first.new, ["src/a.c", "src/b.c"])
            self.assertEqual(first.changed, [])
            self.assertEqual(first.deleted, [])

            os.utime(a, ns=(a.stat().st_atime_ns + 1_000_000, a.stat().st_mtime_ns + 1_000_000))
            touched = census.scan(root, ["src/**/*.c"], previous=first)
            self.assertEqual(touched.changed, [])
            self.assertEqual(touched.new, [])
            self.assertEqual(touched.deleted, [])
            self.assertEqual(touched.digest, first.digest)

            a.write_text("int aa;\n", encoding="utf-8")
            (root / "src" / "new.c").write_text("int n;\n", encoding="utf-8")
            b.unlink()
            updated = census.scan(root, ["src/**/*.c"], previous=touched)

            self.assertEqual(updated.changed, ["src/a.c"])
            self.assertEqual(updated.new, ["src/new.c"])
            self.assertEqual(updated.deleted, ["src/b.c"])
            self.assertNotEqual(updated.digest, first.digest)

    def test_scope_globs_are_relative_and_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "deep").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "src" / "main.c").write_text("main\n", encoding="utf-8")
            (root / "src" / "deep" / "lib.c").write_text("lib\n", encoding="utf-8")
            (root / "src" / "deep" / "lib.h").write_text("lib\n", encoding="utf-8")
            (root / "tests" / "main_test.c").write_text("test\n", encoding="utf-8")

            result = census.scan(root, ["src/**/*.c", "tests/*.c"])

            self.assertEqual(
                list(result.files),
                ["src/deep/lib.c", "src/main.c", "tests/main_test.c"],
            )
            again = census.scan(root, ["tests/*.c", "src/**/*.c"])
            self.assertEqual(again.digest, result.digest)


if __name__ == "__main__":
    unittest.main()

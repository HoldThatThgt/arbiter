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

    def test_double_star_matches_zero_or_more_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "x" / "y").mkdir(parents=True)
            (root / "root.txt").write_text("r\n", encoding="utf-8")
            (root / "src" / "direct.c").write_text("d\n", encoding="utf-8")
            (root / "src" / "x" / "y" / "z.c").write_text("z\n", encoding="utf-8")

            everything = census.scan(root, ["**/*"])
            self.assertEqual(
                list(everything.files),
                ["root.txt", "src/direct.c", "src/x/y/z.c"],
            )

            recursive = census.scan(root, ["src/**/*.c"])
            self.assertEqual(list(recursive.files), ["src/direct.c", "src/x/y/z.c"])

            trailing = census.scan(root, ["src/**"])
            self.assertEqual(list(trailing.files), ["src/direct.c", "src/x/y/z.c"])

            embedded = census.scan(root, ["src/**/y/*.c"])
            self.assertEqual(list(embedded.files), ["src/x/y/z.c"])

    def test_single_star_is_anchored_and_not_recursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "deep").mkdir(parents=True)
            (root / "src" / "a.c").write_text("a\n", encoding="utf-8")
            (root / "src" / "deep" / "b.c").write_text("b\n", encoding="utf-8")
            (root / "top.c").write_text("t\n", encoding="utf-8")

            shallow = census.scan(root, ["src/*.c"])
            self.assertEqual(list(shallow.files), ["src/a.c"])

            root_only = census.scan(root, ["*"])
            self.assertEqual(list(root_only.files), ["top.c"])

    def test_character_classes_match_within_a_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            for name in ("a.c", "b.c", "z.c"):
                (root / "src" / name).write_text(name + "\n", encoding="utf-8")

            result = census.scan(root, ["src/[ab].c"])
            self.assertEqual(list(result.files), ["src/a.c", "src/b.c"])


if __name__ == "__main__":
    unittest.main()

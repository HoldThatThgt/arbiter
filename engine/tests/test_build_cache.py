import tempfile
import unittest
from pathlib import Path

from arbiter_engine.runs import build_cache
from arbiter_engine.runs import state


class BuildCacheTest(unittest.TestCase):
    def test_no_sources_never_hits_cross_process_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / ".arbiter" / "runs" / "state.sqlite"
            state.init(db)

            build_cache.store(db, root, key="k1", binary="build/app", sources=[])

            self.assertIsNone(build_cache.lookup(db, root, key="k1", sources=[]))

    def test_hit_requires_clean_census_and_existing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / ".arbiter" / "runs" / "state.sqlite"
            src = root / "src" / "a.c"
            binary = root / "build" / "app"
            src.parent.mkdir()
            binary.parent.mkdir()
            src.write_text("int a;\n", encoding="utf-8")
            binary.write_text("bin\n", encoding="utf-8")
            state.init(db)

            stored = build_cache.store(db, root, key="debug:unit", binary="build/app", sources=["src/**/*.c"])
            hit = build_cache.lookup(db, root, key="debug:unit", sources=["src/**/*.c"])

            self.assertIsNotNone(hit)
            self.assertEqual(hit.sources_digest, stored.sources_digest)
            self.assertEqual(hit.binary, "build/app")
            binary.unlink()
            self.assertIsNone(build_cache.lookup(db, root, key="debug:unit", sources=["src/**/*.c"]))

    def test_edit_new_and_deleted_sources_miss_but_touch_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / ".arbiter" / "runs" / "state.sqlite"
            src_dir = root / "src"
            src_dir.mkdir()
            src = src_dir / "a.c"
            binary = root / "build" / "app"
            binary.parent.mkdir()
            src.write_text("int a;\n", encoding="utf-8")
            binary.write_text("bin\n", encoding="utf-8")
            state.init(db)
            build_cache.store(db, root, key="debug:unit", binary="build/app", sources=["src/**/*.c"])

            src.touch()
            self.assertIsNotNone(build_cache.lookup(db, root, key="debug:unit", sources=["src/**/*.c"]))
            src.write_text("int a_changed;\n", encoding="utf-8")
            self.assertIsNone(build_cache.lookup(db, root, key="debug:unit", sources=["src/**/*.c"]))

            build_cache.store(db, root, key="debug:unit", binary="build/app", sources=["src/**/*.c"])
            (src_dir / "b.c").write_text("int b;\n", encoding="utf-8")
            self.assertIsNone(build_cache.lookup(db, root, key="debug:unit", sources=["src/**/*.c"]))

            build_cache.store(db, root, key="debug:unit", binary="build/app", sources=["src/**/*.c"])
            (src_dir / "b.c").unlink()
            self.assertIsNone(build_cache.lookup(db, root, key="debug:unit", sources=["src/**/*.c"]))

    def test_deep_source_edit_invalidates_recursive_glob_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / ".arbiter" / "runs" / "state.sqlite"
            deep = root / "src" / "x" / "y" / "z.c"
            deep.parent.mkdir(parents=True)
            deep.write_text("int z;\n", encoding="utf-8")
            binary = root / "build" / "app"
            binary.parent.mkdir()
            binary.write_text("bin\n", encoding="utf-8")
            state.init(db)

            build_cache.store(db, root, key="debug:unit", binary="build/app", sources=["src/**/*.c"])
            self.assertIsNotNone(build_cache.lookup(db, root, key="debug:unit", sources=["src/**/*.c"]))

            deep.write_text("int z_changed;\n", encoding="utf-8")
            self.assertIsNone(build_cache.lookup(db, root, key="debug:unit", sources=["src/**/*.c"]))

    def test_cache_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / ".arbiter" / "runs" / "state.sqlite"
            src = root / "src" / "a.c"
            binary = root / "build" / "app"
            src.parent.mkdir()
            binary.parent.mkdir()
            src.write_text("int a;\n", encoding="utf-8")
            binary.write_text("bin\n", encoding="utf-8")
            state.init(db)

            build_cache.store(db, root, key="debug:unit", binary="build/app", sources=["src/**/*.c"])
            state.init(db)

            self.assertIsNotNone(build_cache.lookup(db, root, key="debug:unit", sources=["src/**/*.c"]))


if __name__ == "__main__":
    unittest.main()

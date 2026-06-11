import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cipher2.config import CipherConfig, load_config, safe_cipher_path
from cipher2.tools.log import open_log


class ConfigDefaultsTest(unittest.TestCase):
    def test_missing_config_returns_defaults_and_derives_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            config = load_config(target)

            self.assertIsInstance(config, CipherConfig)
            self.assertEqual(config.schema_version, 1)
            self.assertEqual(config.target_repo, target)
            self.assertEqual(config.cipher_dir, target / ".cipher")
            self.assertEqual(config.config_path, target / ".cipher" / "config.yml")
            self.assertEqual(config.storage_snapshot_dir, target / ".cipher" / "snapshots")
            self.assertEqual(config.log_dir, target / ".cipher" / "log")
            self.assertIsNone(config.compile_database_path)
            self.assertIsNone(config.clang_executable)
            self.assertIsNone(config.libclang_library_path)
            self.assertIsNone(config.gcc_executable)
            self.assertEqual(config.clang_args, [])
            self.assertGreaterEqual(config.extractor_worker_count, 1)
            self.assertLessEqual(config.extractor_worker_count, 32)
            self.assertFalse(config.config_path.exists())

    def test_observe_false_does_not_create_cipher_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            config = load_config(target, observe=False)

            self.assertIsNone(config.compile_database_path)
            self.assertIsNone(config.clang_executable)
            self.assertIsNone(config.libclang_library_path)
            self.assertIsNone(config.gcc_executable)
            self.assertEqual(config.clang_args, [])
            self.assertGreaterEqual(config.extractor_worker_count, 1)
            self.assertLessEqual(config.extractor_worker_count, 32)
            self.assertFalse((target / ".cipher").exists())

    def test_to_mapping_serializes_only_persistent_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            mapping = load_config(target, observe=False).to_mapping()
            extractor_worker_count = mapping["extractor"]["worker_count"]

            self.assertEqual(
                mapping,
                {
                    "schema_version": 1,
                    "paths": {"compile_database": None},
                    "extractor": {
                        "worker_count": extractor_worker_count,
                        "code": {"clang_executable": None, "libclang_library": None, "gcc_executable": None, "clang_args": []},
                    },
                    "incremental": {
                        "temporary_enabled": True,
                        "poll_interval_ms": 500,
                        "debounce_ms": 100,
                        "worker_count": 1,
                        "overlay_ttl_seconds": 600,
                        "max_dirty_files": 500,
                    },
                },
            )
            self.assertGreaterEqual(extractor_worker_count, 1)
            self.assertLessEqual(extractor_worker_count, 32)
            self.assertNotIn("defaults", mapping)
            self.assertNotIn("graph", mapping)
            self.assertNotIn("inference", mapping)

    def test_auto_extractor_worker_count_caps_at_32_cpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            with patch("cipher2.config.os.cpu_count", return_value=64):
                config = load_config(target, observe=False)

            self.assertEqual(config.extractor_worker_count, 32)

    def test_safe_cipher_path_returns_generated_path_inside_cipher_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            path = safe_cipher_path(target, "snapshots", "current")

            self.assertEqual(path, (target / ".cipher" / "snapshots" / "current").resolve(strict=False))
            self.assertTrue(path.is_relative_to((target / ".cipher").resolve(strict=False)))

    def test_missing_config_observed_as_default_load_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            load_config(target)

            events = open_log(target).read_events(channel="config").events
            event = next(item for item in events if item.event_name == "config.load")
            self.assertEqual(event.status, "ok")
            self.assertEqual(event.payload["operation"], "load_config")
            self.assertEqual(event.payload["outcome"], "default")
            self.assertEqual(event.payload["has_compile_database"], False)
            self.assertEqual(event.payload["compile_database_scope"], "none")
            self.assertEqual(event.payload["libclang_library_scope"], "none")
            self.assertGreaterEqual(event.payload["extractor_worker_count"], 1)
            self.assertLessEqual(event.payload["extractor_worker_count"], 32)
            self.assertEqual(event.payload["config_exists"], False)


if __name__ == "__main__":
    unittest.main()

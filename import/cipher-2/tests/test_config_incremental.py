import tempfile
import unittest
from pathlib import Path

from cipher2.config import ConfigError, load_config, write_default_config


class ConfigIncrementalTest(unittest.TestCase):
    def test_default_config_exposes_incremental_defaults_and_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            config = load_config(target, observe=False)
            mapping = config.to_mapping()

            self.assertTrue(config.incremental_temporary_enabled)
            self.assertEqual(config.incremental_poll_interval_ms, 500)
            self.assertEqual(config.incremental_debounce_ms, 100)
            self.assertEqual(config.incremental_worker_count, 1)
            self.assertEqual(config.incremental_overlay_ttl_seconds, 600)
            self.assertEqual(config.incremental_max_dirty_files, 500)
            self.assertEqual(
                mapping["incremental"],
                {
                    "temporary_enabled": True,
                    "poll_interval_ms": 500,
                    "debounce_ms": 100,
                    "worker_count": 1,
                    "overlay_ttl_seconds": 600,
                    "max_dirty_files": 500,
                },
            )

    def test_write_and_load_incremental_config_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            write_default_config(
                target,
                incremental={
                    "temporary_enabled": False,
                    "poll_interval_ms": 250,
                    "debounce_ms": 75,
                    "worker_count": 4,
                    "overlay_ttl_seconds": 120,
                    "max_dirty_files": 42,
                },
                observe=False,
            )
            config = load_config(target, observe=False)

            self.assertFalse(config.incremental_temporary_enabled)
            self.assertEqual(config.incremental_poll_interval_ms, 250)
            self.assertEqual(config.incremental_debounce_ms, 75)
            self.assertEqual(config.incremental_worker_count, 4)
            self.assertEqual(config.incremental_overlay_ttl_seconds, 120)
            self.assertEqual(config.incremental_max_dirty_files, 42)

    def test_invalid_incremental_ranges_are_rejected(self):
        cases = [
            {"temporary_enabled": "yes"},
            {"poll_interval_ms": 99},
            {"debounce_ms": 49},
            {"worker_count": 0},
            {"overlay_ttl_seconds": 9},
            {"max_dirty_files": 0},
            {"unknown": 1},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for incremental in cases:
                with self.subTest(incremental=incremental):
                    with self.assertRaises(ConfigError) as caught:
                        load_config(target, overrides={"incremental": incremental}, observe=False)
                    self.assertEqual(caught.exception.code, "invalid_config")


if __name__ == "__main__":
    unittest.main()

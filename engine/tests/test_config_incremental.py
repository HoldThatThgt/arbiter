"""New-native arbiter config tests for the live facts.incremental knobs (M4 Phase 2).

cipher-2's test_config_incremental was excluded (its config schema differs); these cover
arbiter's own facts.incremental section + the unified worker knob (owner decisions 2 & 3,
ADR-0018).
"""

import unittest

from arbiter_engine import config


class IncrementalConfigTest(unittest.TestCase):
    def test_defaults_mirror_cipher2_incremental_defaults(self):
        parsed = config.parse_config("facts:\n")
        incremental = parsed.facts.incremental
        self.assertTrue(incremental.enabled)
        self.assertEqual(incremental.poll_interval_ms, 500)
        self.assertEqual(incremental.debounce_ms, 100)
        self.assertEqual(incremental.overlay_ttl_seconds, 600)
        self.assertEqual(incremental.max_dirty_files, 500)
        # worker_count is not a facts.incremental key: it is unified with index_on_build.pool.
        self.assertFalse(hasattr(incremental, "worker_count"))

    def test_empty_config_uses_defaults(self):
        # No file/section at all still yields the live defaults (background index on by default).
        self.assertTrue(config.parse_config("").facts.incremental.enabled)

    def test_each_live_knob_is_overridable(self):
        parsed = config.parse_config(
            "facts:\n"
            "  incremental:\n"
            "    enabled: false\n"
            "    poll_interval_ms: 250\n"
            "    debounce_ms: 25\n"
            "    overlay_ttl_seconds: 0\n"
            "    max_dirty_files: 32\n"
        )
        incremental = parsed.facts.incremental
        self.assertFalse(incremental.enabled)
        self.assertEqual(incremental.poll_interval_ms, 250)
        self.assertEqual(incremental.debounce_ms, 25)
        self.assertEqual(incremental.overlay_ttl_seconds, 0)  # 0 = overlay GC disabled
        self.assertEqual(incremental.max_dirty_files, 32)

    def test_worker_count_is_the_unified_pool(self):
        # Owner decision 2: one knob (facts.index_on_build.pool) drives both build-tail and
        # incremental dirty re-extraction; there is no separate incremental worker_count.
        parsed = config.parse_config("facts:\n  index_on_build:\n    pool: 6\n")
        self.assertEqual(parsed.facts.index_on_build.pool, 6)

    def test_intervals_must_be_positive_and_ttl_non_negative(self):
        for text, detail in (
            ("facts:\n  incremental:\n    poll_interval_ms: 0\n", "positive"),
            ("facts:\n  incremental:\n    debounce_ms: -5\n", "positive"),
            ("facts:\n  incremental:\n    max_dirty_files: 0\n", "positive"),
            ("facts:\n  incremental:\n    overlay_ttl_seconds: -1\n", "non-negative"),
            ("facts:\n  incremental:\n    enabled: maybe\n", "boolean"),
            ("facts:\n  incremental:\n    nope: 1\n", "unknown key"),
            ("facts:\n  incremental: true\n", "mapping"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError) as raised:
                    config.parse_config(text)
                self.assertIn(detail, str(raised.exception))


if __name__ == "__main__":
    unittest.main()

import tempfile
import textwrap
import unittest
from pathlib import Path

from arbiter_engine import config


class ConfigParserTest(unittest.TestCase):
    def test_parses_supported_sections(self):
        parsed = config.parse_config(
            textwrap.dedent(
                """\
                # committed engine config
                facts:
                  extractor: clang
                  incremental:
                    enabled: true
                    poll_interval_ms: 250
                    max_dirty_files: 64
                  index_on_build:
                    pool: 4
                    key_flags: [-fsanitize=address, "__SANITIZE_ADDRESS__"]
                runs:
                match:
                  goal_memo: false
                engine:
                """
            )
        )

        self.assertEqual(parsed.facts.extractor, "clang")
        self.assertTrue(parsed.facts.incremental.enabled)
        self.assertEqual(parsed.facts.incremental.poll_interval_ms, 250)
        self.assertEqual(parsed.facts.incremental.max_dirty_files, 64)
        # Unspecified knobs fall back to the cipher-2 defaults.
        self.assertEqual(parsed.facts.incremental.debounce_ms, 100)
        self.assertEqual(parsed.facts.incremental.overlay_ttl_seconds, 600)
        self.assertEqual(parsed.facts.index_on_build.pool, 4)
        self.assertEqual(
            parsed.facts.index_on_build.key_flags,
            ("-fsanitize=address", "__SANITIZE_ADDRESS__"),
        )
        self.assertEqual(parsed.runs, {})
        self.assertFalse(parsed.match.goal_memo)
        self.assertEqual(parsed.engine, {})

    def test_parses_toolchain_section(self):
        parsed = config.parse_config(
            textwrap.dedent(
                """\
                facts:
                  toolchain:
                    clang: /usr/lib/llvm-16/bin/clang
                    libclang: /usr/lib/llvm-16/lib/libclang.so
                    clang_args: [--gcc-toolchain=/opt/gcc-7.3.0, "-isystem/opt/x"]
                """
            )
        )

        self.assertEqual(parsed.facts.toolchain.clang, "/usr/lib/llvm-16/bin/clang")
        self.assertEqual(parsed.facts.toolchain.libclang, "/usr/lib/llvm-16/lib/libclang.so")
        self.assertEqual(
            parsed.facts.toolchain.clang_args,
            ("--gcc-toolchain=/opt/gcc-7.3.0", "-isystem/opt/x"),
        )

    def test_toolchain_defaults_when_absent(self):
        parsed = config.parse_config("facts:\n  extractor: clang\n")

        self.assertIsNone(parsed.facts.toolchain.clang)
        self.assertIsNone(parsed.facts.toolchain.libclang)
        self.assertEqual(parsed.facts.toolchain.clang_args, ())

    def test_load_config_reads_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yml"
            path.write_text("match:\n  goal_memo: true\n", encoding="utf-8")

            parsed = config.load_config(path)

        self.assertTrue(parsed.match.goal_memo)

    def test_unknown_keys_fail_closed_with_line_numbers(self):
        cases = [
            ("extra:\n", 1, "unknown key"),
            ("facts:\n  unknown: true\n", 2, "unknown key"),
            ("facts:\n  index_on_build:\n    nope: true\n", 3, "unknown key"),
            ("facts:\n  toolchain:\n    nope: x\n", 3, "unknown key"),
        ]

        for text, line, detail in cases:
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError) as raised:
                    config.parse_config(text)

                self.assertEqual(raised.exception.line, line)
                self.assertIn(detail, str(raised.exception))

    def test_hostile_yaml_features_fail_closed(self):
        cases = [
            ("facts:\n\tincremental: true\n", 2, "tabs"),
            ("facts: &defaults\n  incremental: true\n", 1, "unsupported"),
            ("facts:\n  index_on_build:\n    key_flags:\n      - -fsanitize=address\n", 4, "mapping"),
            ("facts:\n  incremental: true\n", 2, "mapping"),
            ("facts:\n  incremental:\n    enabled: maybe\n", 3, "boolean"),
            ("facts:\n  incremental:\n    poll_interval_ms: 0\n", 3, "positive"),
            ("facts:\n  incremental:\n    overlay_ttl_seconds: -1\n", 3, "non-negative"),
            ("facts:\n  incremental:\n    nope: 1\n", 3, "unknown key"),
            ("facts:\n  incremental: true\n  incremental: false\n", 3, "duplicate"),
            ("facts:\n  toolchain:\n    clang: 5\n", 3, "string"),
            ("facts:\n  toolchain:\n    clang_args: not-a-list\n", 3, "inline list"),
        ]

        for text, line, detail in cases:
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError) as raised:
                    config.parse_config(text)

                self.assertEqual(raised.exception.line, line)
                self.assertIn(detail, str(raised.exception))


if __name__ == "__main__":
    unittest.main()

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
                  incremental: true
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
        self.assertTrue(parsed.facts.incremental)
        self.assertEqual(parsed.facts.index_on_build.pool, 4)
        self.assertEqual(
            parsed.facts.index_on_build.key_flags,
            ("-fsanitize=address", "__SANITIZE_ADDRESS__"),
        )
        self.assertEqual(parsed.runs, {})
        self.assertFalse(parsed.match.goal_memo)
        self.assertEqual(parsed.engine, {})

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
            ("facts:\n  incremental: maybe\n", 2, "boolean"),
            ("facts:\n  incremental: true\n  incremental: false\n", 3, "duplicate"),
        ]

        for text, line, detail in cases:
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError) as raised:
                    config.parse_config(text)

                self.assertEqual(raised.exception.line, line)
                self.assertIn(detail, str(raised.exception))


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.runs import recipes


class RecipeBookParserTest(unittest.TestCase):
    def test_golden_recipe_book_v2(self):
        base = Path(__file__).resolve().parent / "fixtures" / "recipes"
        book = recipes.parse((base / "v2_basic.yaml").read_text(encoding="utf-8"))

        self.assertEqual(book.to_json(), json.loads((base / "v2_basic.json").read_text(encoding="utf-8")))
        self.assertEqual(book.target("unit").harness.kind, "gtest")

    def test_rejects_unknown_keys_and_unsupported_yaml(self):
        with self.assertRaisesRegex(recipes.RecipeError, "line 2: unknown key 'mystery'"):
            recipes.parse("vars: {}\nmystery: nope\ntargets: []\n")
        with self.assertRaisesRegex(recipes.RecipeError, "targets entries must be a mapping"):
            recipes.parse("targets:\n  - just-a-string\n")
        with self.assertRaisesRegex(recipes.RecipeError, "anchors are not supported"):
            recipes.parse("vars:\n  build: &build build\n")

    def test_rejects_absolute_portability_paths(self):
        text = """
targets:
  - id: bad
    binary: /tmp/unit
    harness:
      kind: gtest
    test_run:
      cmd: [/tmp/unit]
"""
        with self.assertRaisesRegex(recipes.RecipeError, "binary must be relative"):
            recipes.parse(text)

    def test_load_from_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipes.yaml"
            path.write_text(
                """
targets:
  - id: smoke
    binary: build/smoke
    harness:
      kind: gtest
    test_run:
      cmd: [build/smoke]
""",
                encoding="utf-8",
            )

            book = recipes.load(path)

        self.assertEqual([target.id for target in book.targets], ["smoke"])


if __name__ == "__main__":
    unittest.main()

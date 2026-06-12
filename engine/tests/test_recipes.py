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

    def test_rejects_path_unsafe_target_ids(self):
        template = """
targets:
  - id: {target_id}
    binary: build/unit
    harness:
      kind: gtest
    test_run:
      cmd: [build/unit]
"""
        for bad_id in ("../evil", "a/b", "a\\b", ".hidden", "a..b", '"with space"'):
            with self.subTest(target_id=bad_id):
                with self.assertRaisesRegex(recipes.RecipeError, "target id"):
                    recipes.parse(template.format(target_id=bad_id))
        for good_id in ("unit", "unit-2", "unit_2", "unit.v2", "UNIT9"):
            with self.subTest(target_id=good_id):
                book = recipes.parse(template.format(target_id=good_id))
                self.assertEqual(book.targets[0].id, good_id)

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

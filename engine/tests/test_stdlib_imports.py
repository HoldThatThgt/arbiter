import tempfile
import textwrap
import unittest
from pathlib import Path

from import_policy import find_import_violations


class ImportPolicySelfTest(unittest.TestCase):
    def test_detects_non_stdlib_import_with_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "bad.py"
            module.write_text("import requests\n", encoding="utf-8")

            violations = find_import_violations(root)

        self.assertEqual(len(violations), 1)
        self.assertIn("bad.py:1", str(violations[0]))
        self.assertIn("requests", str(violations[0]))

    def test_relative_import_is_intra_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "ok.py"
            module.write_text("from . import sibling\n", encoding="utf-8")

            violations = find_import_violations(root)

        self.assertEqual(violations, [])

    def test_scan_extra_import_must_be_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "runs" / "scan.py"
            module.parent.mkdir()
            module.write_text("import tree_sitter\n", encoding="utf-8")

            violations = find_import_violations(root)

        self.assertEqual(len(violations), 1)
        self.assertIn("runs/scan.py:1", str(violations[0]))
        self.assertIn("guarded", str(violations[0]))

    def test_guarded_scan_extra_import_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "runs" / "scan.py"
            module.parent.mkdir()
            module.write_text(
                textwrap.dedent(
                    """\
                    try:
                        import tree_sitter
                    except ImportError:
                        tree_sitter = None
                    """
                ),
                encoding="utf-8",
            )

            violations = find_import_violations(root)

        self.assertEqual(violations, [])


class EngineStdlibImportsTest(unittest.TestCase):
    def test_engine_imports_are_stdlib_only(self):
        root = Path(__file__).resolve().parents[1] / "arbiter_engine"

        violations = find_import_violations(root)

        self.assertEqual(
            violations,
            [],
            "non-stdlib imports found:\n"
            + "\n".join(textwrap.indent(str(v), "  ") for v in violations),
        )


if __name__ == "__main__":
    unittest.main()

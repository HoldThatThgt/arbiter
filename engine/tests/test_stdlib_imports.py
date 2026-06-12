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

    def test_detects_importlib_import_module_non_stdlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "bad.py"
            module.write_text(
                textwrap.dedent(
                    """\
                    import importlib

                    importlib.import_module("requests")
                    """
                ),
                encoding="utf-8",
            )

            violations = find_import_violations(root)

        self.assertEqual(len(violations), 1)
        self.assertIn("bad.py:3", str(violations[0]))
        self.assertIn("requests", str(violations[0]))

    def test_detects_dunder_import_non_stdlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "bad.py"
            module.write_text('__import__("requests")\n', encoding="utf-8")

            violations = find_import_violations(root)

        self.assertEqual(len(violations), 1)
        self.assertIn("bad.py:1", str(violations[0]))
        self.assertIn("requests", str(violations[0]))

    def test_allows_c_extension_stdlib_modules(self):
        # resource lives in lib-dynload/ as a C extension on POSIX; the 3.9
        # fallback stdlib scan must still recognize it (perfmcp imports it).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "ok.py"
            module.write_text("import resource\n", encoding="utf-8")

            violations = find_import_violations(root)

        self.assertEqual(violations, [])

    def test_non_literal_dynamic_import_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "bad.py"
            module.write_text(
                textwrap.dedent(
                    """\
                    import importlib

                    name = "requ" + "ests"
                    importlib.import_module(name)
                    __import__(name)
                    importlib.__import__(name)
                    """
                ),
                encoding="utf-8",
            )

            violations = find_import_violations(root)

        self.assertEqual(len(violations), 3)
        for line, violation in zip((4, 5, 6), violations):
            self.assertIn(f"bad.py:{line}", str(violation))
            self.assertIn("non-literal module name", str(violation))


    def test_allows_stdlib_dynamic_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "ok.py"
            module.write_text(
                textwrap.dedent(
                    """\
                    import importlib

                    importlib.import_module("json")
                    __import__("pathlib")
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

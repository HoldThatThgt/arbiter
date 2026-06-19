"""Build-independent AST scan (runs.scan) and its union with the facts index.

The real tree-sitter scan only runs when the optional ``[scan]`` extra is
installed, so those cases are guarded with ``skipUnless``. The degrade path —
tree-sitter absent, ``scan`` falling back to the facts-only inventory — is
exercised unconditionally (it is the default in a stdlib-only install).
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from arbiter_engine import rpc
from arbiter_engine.facts.store import FactRecord, open_fact_store
from arbiter_engine.runs import discovery
from arbiter_engine.runs import scan as ast_scan


_GTEST_SOURCE = b"""
#include <gtest/gtest.h>

TEST(MathSuite, Adds) {
  EXPECT_EQ(1 + 1, 2);
}

TEST_F(MathFixture, Multiplies) {
  EXPECT_EQ(2 * 3, 6);
}

TEST_P(ParamSuite, Handles) {
  EXPECT_TRUE(true);
}

TYPED_TEST(TypedSuite, Works) {
  EXPECT_TRUE(true);
}

INSTANTIATE_TEST_SUITE_P(MyPrefix, ParamSuite, ::testing::Values(1, 2, 3));
"""


def _test_body_fact(suite, name, file, line, fact_id):
    return FactRecord(
        object_id=fact_id,
        object_name=f"{suite}_{name}_Test",
        object_description=f"gtest fixture {suite}.{name}",
        object_source=f"{file}:{line}",
        object_profile="debug",
        payload={"fact_kind": "type"},
    )


def _publish(root, facts):
    open_fact_store(root, mode="w", log_enabled=False).replace_snapshot(facts, [], [])


def _write(root, relpath, data=_GTEST_SOURCE):
    path = Path(root) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@unittest.skipUnless(
    ast_scan.tree_sitter_available(),
    "tree-sitter [scan] extra not installed",
)
class AstScanTest(unittest.TestCase):
    def test_scan_sources_finds_every_declared_macro(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "src/calc_test.cc")

            declared = ast_scan.scan_sources(tmp)

            by_test = {(d.suite, d.name): d for d in declared}
            self.assertIn(("MathSuite", "Adds"), by_test)
            self.assertEqual(by_test[("MathSuite", "Adds")].kind, "TEST")
            self.assertIsNone(by_test[("MathSuite", "Adds")].fixture)
            self.assertEqual(by_test[("MathFixture", "Multiplies")].kind, "TEST_F")
            self.assertEqual(by_test[("MathFixture", "Multiplies")].fixture, "MathFixture")
            self.assertEqual(by_test[("ParamSuite", "Handles")].kind, "TEST_P")
            self.assertEqual(by_test[("TypedSuite", "Works")].kind, "TYPED_TEST")
            # The parametrized instantiation is reported as the runnable filter.
            self.assertIn(("MyPrefix/ParamSuite", "*"), by_test)
            # The recorded location is the real source file, relative to root.
            self.assertEqual(by_test[("MathSuite", "Adds")].file, "src/calc_test.cc")
            self.assertGreater(by_test[("MathSuite", "Adds")].line, 0)

    def test_scan_is_build_independent(self):
        # No facts snapshot at all — discovery still finds the declared tests.
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "calc_test.cc")
            declared = discovery.discover_declared_tests(tmp)
            self.assertIn(("MathSuite", "Adds"), {(d.suite, d.name) for d in declared})
            self.assertTrue(all(d.fact_id == "" for d in declared))

    def test_scan_unions_declared_with_built_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                tmp,
                "calc_test.cc",
                b"""
#include <gtest/gtest.h>
TEST(Lock, Deadlock) { SUCCEED(); }
TEST(Suite, Fail) { SUCCEED(); }
""",
            )
            # Only Suite.Fail built; plus a macro-generated case the AST can't see.
            _publish(
                tmp,
                [
                    _test_body_fact("Suite", "Fail", "calc_test.cc", 4, "code:function:built"),
                    _test_body_fact("Gen", "Erated", "gen.cc", 9, "code:function:genonly"),
                ],
            )

            result = discovery.scan(tmp, "*")
            by_test = {(c.suite, c.name): c for c in result}

            # Declared-but-unbuilt: present, no fact, built == False.
            self.assertIn(("Lock", "Deadlock"), by_test)
            self.assertFalse(by_test[("Lock", "Deadlock")].built)
            self.assertEqual(by_test[("Lock", "Deadlock")].kind, "TEST")
            # Declared AND built: AST kind preserved, fact_id attached, built True.
            self.assertTrue(by_test[("Suite", "Fail")].built)
            self.assertEqual(by_test[("Suite", "Fail")].fact_id, "code:function:built")
            self.assertEqual(by_test[("Suite", "Fail")].kind, "TEST")
            # Facts-only (no source): still surfaced, marked built, no macro kind.
            self.assertIn(("Gen", "Erated"), by_test)
            self.assertTrue(by_test[("Gen", "Erated")].built)
            self.assertEqual(by_test[("Gen", "Erated")].kind, "")

    def test_scan_tool_reports_ast_discovery_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "calc_test.cc", b"#include <gtest/gtest.h>\nTEST(A, B) { SUCCEED(); }\n")

            response = _response_for(_tool_call("scan", {"scope": "*"}), tmp)
            structured = response["result"]["structuredContent"]

            self.assertEqual(structured["discovery"], "ast")
            self.assertNotIn("scan_unavailable", structured)
            tests = {t["test"] for t in structured["targets"]}
            self.assertIn("A.B", tests)


class DegradeWithoutTreeSitterTest(unittest.TestCase):
    """With tree-sitter absent, scan falls back to the facts-only inventory."""

    def setUp(self):
        self._saved = ast_scan._IMPORT_ERROR
        ast_scan._IMPORT_ERROR = "simulated: no tree_sitter"

    def tearDown(self):
        ast_scan._IMPORT_ERROR = self._saved

    def test_scan_sources_raises_typed_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ast_scan.ScanUnavailable):
                ast_scan.scan_sources(tmp)
        self.assertEqual(ast_scan.unavailable_reason(), "tree_sitter_not_installed")
        self.assertFalse(ast_scan.tree_sitter_available())

    def test_declared_discovery_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "calc_test.cc")  # has TESTs, but no scanner to read them
            self.assertEqual(discovery.discover_declared_tests(tmp), ())

    def test_scan_falls_back_to_facts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "calc_test.cc")  # declared tests are invisible without AST
            _publish(tmp, [_test_body_fact("Built", "Only", "b.cc", 1, "code:function:b")])

            result = discovery.scan(tmp, "*")

            self.assertEqual([(c.suite, c.name) for c in result], [("Built", "Only")])
            self.assertTrue(result[0].built)

    def test_scan_tool_surfaces_typed_unavailable_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = _response_for(_tool_call("scan", {"scope": "*"}), tmp)
            structured = response["result"]["structuredContent"]
            self.assertEqual(structured["discovery"], "facts")
            self.assertEqual(structured["scan_unavailable"], "tree_sitter_not_installed")


def _tool_call(name, arguments, request_id=1):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        separators=(",", ":"),
    ) + "\n"


def _response_for(line, cwd):
    old = os.getcwd()
    try:
        os.chdir(cwd)
        stdout = io.StringIO()
        rpc.serve(io.StringIO(line), stdout)
        return json.loads(stdout.getvalue())
    finally:
        os.chdir(old)


if __name__ == "__main__":
    unittest.main()

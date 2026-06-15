import tempfile
import unittest
from pathlib import Path

from arbiter_engine.facts.store import FactRecord, open_fact_store
from arbiter_engine.runs import gtest
from arbiter_engine.runs import guidance


FAILED_XML = """
<testsuites tests="1" failures="1">
  <testsuite name="Suite">
    <testcase classname="Suite" name="Fail" time="0.001"><failure message="bad"/></testcase>
  </testsuite>
</testsuites>
"""


def _test_body_fact(suite, name, file, line, fact_id):
    # gtest TEST(Suite, Name) makes the libclang extractor record the generated
    # fixture TYPE `Suite_Name_Test` (not the macro-expanded `::TestBody` method) —
    # the discovery primary key.
    return FactRecord(
        object_id=fact_id,
        object_name=f"{suite}_{name}_Test",
        object_description=f"gtest fixture {suite}.{name}",
        object_source=f"{file}:{line}",
        object_profile="debug",
        payload={"fact_kind": "type"},
    )


class GuidanceTest(unittest.TestCase):
    def test_empty_without_read_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = gtest.parse_xml(self.write_xml(Path(tmp), FAILED_XML), run_id="r1")

            self.assertEqual(guidance.for_result(Path(tmp), result), ())

    def test_guidance_uses_real_read_index_file_line_and_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_index(root, [_test_body_fact("Suite", "Fail", "src/fail.cc", 42, "code:function:abc")])
            result = gtest.parse_xml(self.write_xml(root, FAILED_XML), run_id="r2")

            entries = guidance.for_result(root, result)

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].test, "Suite.Fail")
            self.assertEqual(entries[0].file, "src/fail.cc")
            self.assertEqual(entries[0].line, 42)
            self.assertEqual(
                entries[0].next_queries,
                (
                    "detail code:function:abc",
                    'search "test:Suite.Fail"',
                ),
            )

    def test_guidance_caps_at_four_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = [
                _test_body_fact("Suite", f"Fail{i}", f"f{i}.cc", i + 1, f"code:function:{i}")
                for i in range(6)
            ]
            self.write_index(root, facts)
            xml = "<testsuites>" + "".join(
                f'<testcase classname="Suite" name="Fail{i}"><failure message="bad"/></testcase>'
                for i in range(6)
            ) + "</testsuites>"
            result = gtest.parse_xml(self.write_xml(root, xml), run_id="r3")

            entries = guidance.for_result(root, result)

            self.assertEqual(len(entries), 4)
            self.assertEqual([entry.test for entry in entries], [f"Suite.Fail{i}" for i in range(4)])

    def write_xml(self, root, text):
        path = root / "result.xml"
        path.write_text(text, encoding="utf-8")
        return path

    def write_index(self, root, facts):
        open_fact_store(root, mode="w", log_enabled=False).replace_snapshot(facts, [], [])


if __name__ == "__main__":
    unittest.main()

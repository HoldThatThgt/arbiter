import json
import tempfile
import unittest
from pathlib import Path

from arbiter_engine.runs import gtest
from arbiter_engine.runs import guidance


FAILED_XML = """
<testsuites tests="1" failures="1">
  <testsuite name="Suite">
    <testcase classname="Suite" name="Fail" time="0.001"><failure message="bad"/></testcase>
  </testsuite>
</testsuites>
"""


class GuidanceTest(unittest.TestCase):
    def test_empty_without_read_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = gtest.parse_xml(self.write_xml(Path(tmp), FAILED_XML), run_id="r1")

            self.assertEqual(guidance.for_result(Path(tmp), result), ())

    def test_guidance_uses_stub_read_index_file_line_and_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_index(
                root,
                {
                    "tests": [
                        {
                            "suite": "Suite",
                            "name": "Fail",
                            "file": "src/fail.cc",
                            "line": 42,
                            "detail_id": "code:function:abc",
                        }
                    ]
                },
            )
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
            tests = [
                {"suite": "Suite", "name": f"Fail{i}", "file": f"f{i}.cc", "line": i + 1}
                for i in range(6)
            ]
            self.write_index(root, {"tests": tests})
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

    def write_index(self, root, payload):
        path = root / ".arbiter" / "facts" / "read_index.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

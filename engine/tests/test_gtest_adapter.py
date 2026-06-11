import tempfile
import unittest
from pathlib import Path

from arbiter_engine.runs import gtest
from arbiter_engine.runs import recipes


class GTestAdapterTest(unittest.TestCase):
    def test_injects_xml_output_and_parses_result_file_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_gtest.sh"
            fake.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > args.log\n"
                "printf 'stdout says everything passed\\n'\n"
                "for arg in \"$@\"; do\n"
                "  case \"$arg\" in\n"
                "    --gtest_output=xml:*) out=\"${arg#--gtest_output=xml:}\" ;;\n"
                "  esac\n"
                "done\n"
                "mkdir -p \"$(dirname \"$out\")\"\n"
                "cat > \"$out\" <<'XML'\n"
                "<testsuites tests=\"2\" failures=\"1\" skipped=\"0\">\n"
                "  <testsuite name=\"Suite\" tests=\"2\" failures=\"1\" skipped=\"0\">\n"
                "    <testcase classname=\"Suite\" name=\"Pass\" time=\"0.001\"/>\n"
                "    <testcase classname=\"Suite\" name=\"Fail\" time=\"0.002\"><failure message=\"bad\">trace</failure></testcase>\n"
                "  </testsuite>\n"
                "</testsuites>\n"
                "XML\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            book = recipes.parse(
                f"""
targets:
  - id: unit
    binary: fake_gtest.sh
    harness:
      kind: gtest
    test_run:
      cmd: [{str(fake)}]
"""
            )

            result = gtest.run_target(root, book, "unit", run_id="r1")

            self.assertEqual(result.overall, "failed")
            self.assertEqual((result.passed, result.failed, result.skipped), (1, 1, 0))
            self.assertEqual(result.per_test[1].message, "bad")
            self.assertIn("--gtest_output=xml:", (root / "args.log").read_text(encoding="utf-8"))

    def test_repeated_names_get_occurrences(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "repeated.xml"
            xml.write_text(
                """
<testsuites tests="2" failures="0" skipped="0">
  <testsuite name="Suite" tests="2" failures="0" skipped="0">
    <testcase classname="Suite" name="Same" time="0.001"/>
    <testcase classname="Suite" name="Same" time="0.002"/>
  </testsuite>
</testsuites>
""",
                encoding="utf-8",
            )

            result = gtest.parse_xml(xml, run_id="r2")

            self.assertEqual([case.occurrence for case in result.per_test], [1, 2])
            self.assertEqual(result.overall, "passed")

    def test_empty_suite_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "empty.xml"
            xml.write_text('<testsuites tests="0" failures="0" skipped="0"/>', encoding="utf-8")

            result = gtest.parse_xml(xml, run_id="r3")

            self.assertEqual(result.overall, "passed")
            self.assertEqual((result.passed, result.failed, result.skipped), (0, 0, 0))

    def test_missing_result_file_fails_closed_without_stdout_scrape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_crash.sh"
            fake.write_text("#!/bin/sh\nprintf 'PASSED fake stdout\\n'\nexit 1\n", encoding="utf-8")
            fake.chmod(0o755)
            book = recipes.parse(
                f"""
targets:
  - id: crash
    binary: fake_crash.sh
    harness:
      kind: gtest
    test_run:
      cmd: [{str(fake)}]
"""
            )

            result = gtest.run_target(root, book, "crash", run_id="r4")

            self.assertEqual(result.overall, "failed")
            self.assertEqual(result.failure, "missing_result_file")
            self.assertEqual(result.per_test, ())


if __name__ == "__main__":
    unittest.main()

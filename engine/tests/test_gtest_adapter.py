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

            self.assertEqual(result.overall, "errored")
            self.assertEqual(result.failure, "missing_result_file")
            self.assertEqual(result.per_test, ())

    def test_test_run_timeout_returns_errored_result_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slow = root / "slow_gtest.sh"
            slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
            slow.chmod(0o755)
            book = recipes.parse(
                f"""
targets:
  - id: slow
    binary: slow_gtest.sh
    harness:
      kind: gtest
    test_run:
      cmd: [{str(slow)}]
      timeout_s: 1
"""
            )

            result = gtest.run_target(root, book, "slow", run_id="r-timeout")

            self.assertEqual(result.overall, "errored")
            self.assertEqual(result.failure, "timeout")
            self.assertIn("timeout", result.stderr_tail)

    def test_harness_timeout_override_beats_stage_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slow = root / "slow_gtest.sh"
            slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
            slow.chmod(0o755)
            book = recipes.parse(
                f"""
targets:
  - id: slow
    binary: slow_gtest.sh
    harness:
      kind: gtest
    test_run:
      cmd: [{str(slow)}]
      timeout_s: 600
"""
            )

            result = gtest.run_target(root, book, "slow", run_id="r-override", timeout_s=1)

            self.assertEqual(result.overall, "errored")
            self.assertEqual(result.failure, "timeout")

    def test_workdir_escape_is_an_errored_result_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            book = recipes.parse(
                """
targets:
  - id: unit
    binary: build/unit
    workdir: ../outside
    harness:
      kind: gtest
    test_run:
      cmd: [build/unit]
"""
            )

            result = gtest.run_target(root, book, "unit", run_id="r-escape")

            self.assertEqual(result.overall, "errored")
            self.assertEqual(result.failure, "workdir_escape")
            self.assertIn("escapes the repo root", result.stderr_tail)

    def test_compile_failure_is_errored_never_a_red_run(self):
        # A build that does not compile yields overall="errored", never "failed".
        # The distinction is load-bearing for the referee: a red gate
        # (expect overall=failed) certifies that a test RAN and asserted false,
        # and a broken build never ran - so it must satisfy neither the red gate
        # nor the green one. Were a compile failure "failed", a non-compiling test
        # would pass a run-red predicate (e.g. fix-reported-bug's repro gate).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.c").write_text("not valid C\n", encoding="utf-8")
            book = recipes.parse(
                """
compile_db:
  path: compile_commands.json
targets:
  - id: unit
    binary: build/unit
    harness:
      kind: gtest
    src_compile:
      cmd: [/bin/sh, -c, "exit 7"]
    test_run:
      cmd: [/bin/false]
"""
            )

            result = gtest.run_target(root, book, "unit", run_id="r-compile-fail")

            self.assertEqual(result.overall, "errored")
            self.assertEqual(result.failure, "src_compile")
            self.assertEqual(result.per_test, ())

    def test_zero_tests_matched_is_errored_not_passed(self):
        # A `tests` filter that matches nothing makes gtest exit 0 with an empty
        # result file, which parse_xml reads as "passed". run_target overrides
        # that to "errored": the recipe obtained no verdict, so a green gate
        # (expect overall=passed) cannot be satisfied by a tests override that
        # names a case the binary does not contain - a typo, or a symptom test
        # never compiled into the suite.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "empty_gtest.sh"
            fake.write_text(
                "#!/bin/sh\n"
                'for arg in "$@"; do\n'
                '  case "$arg" in --gtest_output=xml:*) out="${arg#--gtest_output=xml:}" ;; esac\n'
                "done\n"
                'mkdir -p "$(dirname "$out")"\n'
                'printf \'<testsuites tests="0" failures="0" skipped="0"/>\' > "$out"\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            book = recipes.parse(
                f"""
targets:
  - id: unit
    binary: empty_gtest.sh
    harness:
      kind: gtest
    test_run:
      cmd: [{str(fake)}]
"""
            )

            result = gtest.run_target(root, book, "unit", run_id="r-zero")

            self.assertEqual(result.overall, "errored")
            self.assertEqual(result.failure, "no_tests_ran")
            self.assertEqual((result.passed, result.failed, result.skipped), (0, 0, 0))
            self.assertFalse((Path(tmp) / "outside").exists())

    def test_src_compile_publishes_facts_and_sanitizer_profile_reextracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.c").write_text("int a(void) { return 1; }\n", encoding="utf-8")
            fake_arbiter = root / "fake_arbiter.py"
            fake_cc = root / "fake_cc.sh"
            fake_gtest = root / "fake_gtest.sh"
            self.write_fake_arbiter(fake_arbiter)
            self.write_fake_cc(fake_cc)
            self.write_fake_gtest(fake_gtest)
            book = recipes.parse(
                f"""
profiles:
  asan:
    cflags_append: [-fsanitize=address]
compile_db:
  path: compile_commands.json
targets:
  - id: unit
    binary: build/unit
    harness:
      kind: gtest
    src_compile:
      cmd: [/bin/sh, -c, "$CC $CFLAGS -Iinclude -O2 -c src/a.c -o build/a.o"]
      env:
        CC: {str(fake_cc)}
    test_run:
      cmd: [{str(fake_gtest)}]
"""
            )
            extracted = []

            def extractor(unit):
                extracted.append(unit.source)
                return {"warnings": []}

            plain = gtest.run_target(
                root,
                book,
                "unit",
                run_id="plain",
                arbiter_bin=str(fake_arbiter),
                facts_extractor=extractor,
            )
            asan = gtest.run_target(
                root,
                book,
                "unit",
                run_id="asan",
                profiles=["asan"],
                arbiter_bin=str(fake_arbiter),
                facts_extractor=extractor,
            )

            self.assertEqual(plain.overall, "passed")
            self.assertEqual(asan.overall, "passed")
            self.assertTrue(plain.facts["published"])
            self.assertTrue(asan.to_json()["facts"]["published"])
            # -fsanitize=* is semantic (sanitizer macros change preprocessor
            # state), so the asan profile must re-extract rather than hit the
            # plain-build extract cache (ADR-0005).
            source = str((root / "src" / "a.c").resolve())
            self.assertEqual(extracted, [source, source])
            self.assertIn("-fsanitize=address", (root / "cflags.log").read_text(encoding="utf-8"))

    def write_fake_arbiter(self, path):
        path.write_text(
            """#!/usr/bin/env python3
import json
import os
import subprocess
import sys

if len(sys.argv) < 4 or sys.argv[1:3] != ["cc", "--"]:
    sys.exit(2)
argv = sys.argv[3:]
src = ""
out = ""
for index, arg in enumerate(argv):
    if arg.endswith((".c", ".cc", ".cpp", ".cxx")) and not src:
        src = arg
    if arg == "-o" and index + 1 < len(argv):
        out = argv[index + 1]
path = os.path.join(
    os.getcwd(),
    ".arbiter",
    "facts",
    "run",
    "compile-journal.%s.jsonl" % os.environ.get("ARBITER_BUILD_ID", "default"),
)
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "src": src, "out": out}, separators=(",", ":")) + "\\n")
sys.exit(subprocess.run(argv).returncode)
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def write_fake_cc(self, path):
        path.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$CFLAGS\" >> cflags.log\n"
            "mkdir -p build\n"
            "touch build/a.o\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def write_fake_gtest(self, path):
        path.write_text(
            "#!/bin/sh\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in --gtest_output=xml:*) out=\"${arg#--gtest_output=xml:}\" ;; esac\n"
            "done\n"
            "mkdir -p \"$(dirname \"$out\")\"\n"
            "cat > \"$out\" <<'XML'\n"
            "<testsuites tests=\"1\" failures=\"0\"><testsuite name=\"Suite\"><testcase classname=\"Suite\" name=\"Pass\" time=\"0.001\"/></testsuite></testsuites>\n"
            "XML\n",
            encoding="utf-8",
        )
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()

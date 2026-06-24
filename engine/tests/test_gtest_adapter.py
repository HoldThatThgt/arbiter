import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arbiter_engine.facts.extractor.code._shim import InitError
from arbiter_engine.runs import gtest
from arbiter_engine.runs import recipes

# Importing the c2 package installs the JSON-AST libclang oracle (its __init__ side-effect), so the
# now-mandatory facts publish inside run_target extracts hermetically — no real libclang/clang.
from c2.toolchain_helpers import write_fake_toolchain


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

    def test_count_listed_tests_parses_gtest_listing(self):
        # A flush-left line is a suite header; an indented line is a test case.
        listing = "Suite.\n  CaseA\n  CaseB\nOther/Typed.\n  Foo/0  # GetParam() = 1\n"
        self.assertEqual(gtest._count_listed_tests(listing), 3)
        # A non-gtest true/echo prints nothing (or a flush-left line) -> 0, which fails
        # the build-booted listed_tests_min:1 floor.
        self.assertEqual(gtest._count_listed_tests(""), 0)
        self.assertEqual(gtest._count_listed_tests("not a gtest binary\n"), 0)

    def test_boot_enumerate_missing_binary_is_none(self):
        # `binary:` does not resolve to a file -> no boot evidence -> the verdict fails closed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(gtest._boot_enumerate(root / "nope", root, {}, 5), (None, None))

    def test_boot_datum_records_exit_and_listed_count(self):
        # run_target runs `<binary:> --gtest_list_tests` itself and stamps its exit + count onto
        # the result, so the build-booted predicate can read a real launch+enumerate datum.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_gtest.sh"
            fake.write_text(
                "#!/bin/sh\n"
                "for arg in \"$@\"; do\n"
                "  case \"$arg\" in\n"
                "    --gtest_list_tests) printf 'Suite.\\n  Case1\\n  Case2\\n  Case3\\n'; exit 0 ;;\n"
                "  esac\n"
                "done\n"
                "for arg in \"$@\"; do\n"
                "  case \"$arg\" in\n"
                "    --gtest_output=xml:*) out=\"${arg#--gtest_output=xml:}\" ;;\n"
                "  esac\n"
                "done\n"
                "mkdir -p \"$(dirname \"$out\")\"\n"
                "cat > \"$out\" <<'XML'\n"
                "<testsuites tests=\"1\" failures=\"0\" skipped=\"0\">\n"
                "  <testsuite name=\"Suite\" tests=\"1\" failures=\"0\" skipped=\"0\">\n"
                "    <testcase classname=\"Suite\" name=\"Case1\" time=\"0.001\"/>\n"
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

            # The build-booted gate runs under the no-match BOOT_FILTER; boot is captured there.
            result = gtest.run_target(root, book, "unit", run_id="boot", tests=[gtest.BOOT_FILTER])
            self.assertEqual(result.boot_exit_code, 0)
            self.assertEqual(result.listed_tests, 3)
            self.assertEqual(result.to_json()["boot_exit_code"], 0)
            self.assertEqual(result.to_json()["listed_tests"], 3)

            # Any other filter (candidate-proven's ["*"], cover's ["Suite.*"]) does NOT consume boot,
            # so the dedicated enumeration is skipped — no wasted subprocess, fields stay None.
            other = gtest.run_target(root, book, "unit", run_id="nonboot", tests=["Suite.Case1"])
            self.assertIsNone(other.boot_exit_code)
            self.assertIsNone(other.listed_tests)
            self.assertNotIn("boot_exit_code", other.to_json())

    def test_test_run_pre_runs_before_the_binary(self):
        # Runtime setup declared in test_run.pre (start a service, generate config/data) MUST run
        # before the test binary. The fake gtest reports pass only if pre created the sentinel.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_gtest.sh"
            fake.write_text(
                "#!/bin/sh\n"
                "for arg in \"$@\"; do case \"$arg\" in --gtest_output=xml:*) out=\"${arg#--gtest_output=xml:}\";; esac; done\n"
                "mkdir -p \"$(dirname \"$out\")\"\n"
                "if [ \"$(cat setupdone 2>/dev/null)\" = ready ]; then\n"
                "  printf '%s' '<testsuites tests=\"1\" failures=\"0\" skipped=\"0\"><testsuite name=\"S\" tests=\"1\" failures=\"0\" skipped=\"0\"><testcase classname=\"S\" name=\"OK\" time=\"0\"/></testsuite></testsuites>' > \"$out\"\n"
                "else\n"
                "  printf '%s' '<testsuites tests=\"1\" failures=\"1\" skipped=\"0\"><testsuite name=\"S\" tests=\"1\" failures=\"1\" skipped=\"0\"><testcase classname=\"S\" name=\"OK\" time=\"0\"><failure message=\"setup missing\">x</failure></testcase></testsuite></testsuites>' > \"$out\"\n"
                "fi\n"
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
      pre:
        - [sh, -c, "echo ready > setupdone"]
      cmd: [{str(fake)}]
"""
            )
            result = gtest.run_target(root, book, "unit", run_id="pre-ok")
            self.assertEqual(result.overall, "passed")
            self.assertTrue((root / "setupdone").exists())

    def test_test_run_pre_failure_is_errored_not_a_run(self):
        # A non-zero test_run.pre means the environment could not be set up: errored
        # (test_run_pre_failed), never a passable run, and the binary must not have run.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_gtest.sh"
            fake.write_text("#!/bin/sh\ntouch ran_anyway\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            book = recipes.parse(
                f"""
targets:
  - id: unit
    binary: fake_gtest.sh
    harness:
      kind: gtest
    test_run:
      pre:
        - [sh, -c, "exit 3"]
      cmd: [{str(fake)}]
"""
            )
            result = gtest.run_target(root, book, "unit", run_id="pre-fail")
            self.assertEqual(result.overall, "errored")
            self.assertEqual(result.failure, "test_run_pre_failed")
            self.assertFalse((root / "ran_anyway").exists())

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

    def test_disabled_testcase_counted_as_skipped_not_passed(self):
        # A DISABLED_ test never runs: gtest emits it as status="notrun" with no
        # <failure>/<error>/<skipped> child. It must count as skipped, not inflate
        # the passed total (and the run still passes since nothing failed).
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "disabled.xml"
            xml.write_text(
                """
<testsuites tests="2" failures="0" disabled="1">
  <testsuite name="Suite" tests="2" failures="0" disabled="1">
    <testcase classname="Suite" name="Runs" status="run" time="0.001"/>
    <testcase classname="Suite" name="DISABLED_Off" status="notrun"/>
  </testsuite>
</testsuites>
""",
                encoding="utf-8",
            )

            result = gtest.parse_xml(xml, run_id="r4")

            self.assertEqual((result.passed, result.failed, result.skipped), (1, 0, 1))
            self.assertEqual(result.overall, "passed")
            statuses = {case.name: case.status for case in result.per_test}
            self.assertEqual(statuses, {"Runs": "passed", "DISABLED_Off": "skipped"})

    def test_disabled_named_test_that_ran_is_not_misreported_as_skipped(self):
        # A DISABLED_-named case that actually executed (e.g. under
        # --gtest_also_run_disabled_tests) reports status="run", not "notrun". The
        # name prefix alone must NOT demote it to skipped — it is a real pass/fail.
        # The status-less form (no `status` attr) is the normal passing shape and
        # must likewise stay passed despite the DISABLED_ name.
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "ran_disabled.xml"
            xml.write_text(
                """
<testsuites tests="2" failures="0">
  <testsuite name="Suite" tests="2" failures="0">
    <testcase classname="Suite" name="DISABLED_Ran" status="run" time="0.001"/>
    <testcase classname="Suite" name="DISABLED_NoStatus" time="0.002"/>
  </testsuite>
</testsuites>
""",
                encoding="utf-8",
            )

            result = gtest.parse_xml(xml, run_id="r5")

            self.assertEqual((result.passed, result.failed, result.skipped), (2, 0, 0))
            statuses = {case.name: case.status for case in result.per_test}
            self.assertEqual(
                statuses, {"DISABLED_Ran": "passed", "DISABLED_NoStatus": "passed"}
            )

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

    def test_indexer_toolchain_failure_is_an_errored_run_not_a_silent_pass(self):
        # Owner policy: the code index is a must-have. When the facts publish raises a toolchain
        # InitError (clang/libclang unusable), run_target aborts with a typed indexer_unavailable
        # errored run rather than reporting a green build with no fact index behind it.
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
      cmd: [/bin/true]
    test_run:
      cmd: [/bin/false]
"""
        )
        boom = InitError(
            "libclang_unavailable",
            "libclang library is unavailable",
            details={"reason": "auto_not_found"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(gtest, "_run_compile_stages", side_effect=boom):
                result = gtest.run_target(root, book, "unit", run_id="r-no-index")

        self.assertEqual(result.overall, "errored")
        self.assertEqual(result.failure, "indexer_unavailable")
        self.assertEqual((result.passed, result.failed, result.skipped), (0, 0, 0))
        # The reason is actionable: it names the failure code and points at the fix.
        self.assertIn("libclang_unavailable", result.stderr_tail)
        self.assertIn("auto_not_found", result.stderr_tail)
        self.assertIn("facts.toolchain", result.stderr_tail)

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

    def test_src_compile_tests_filter_matches_nothing_so_run_is_errored(self):
        # The gear-up-published predicate runs `src_compile` with tests:["src_compile"].
        # On a real recipe whose test_run binary holds actual gtest cases, that filter
        # selects NO test (no suite is named "src_compile"), so gtest writes a zero-test
        # result and run_target reports overall="errored"/no_tests_ran. A gate expecting
        # overall="passed" can therefore never be satisfied — a build proof must assert
        # only facts.published (see the build-published predicate in recipe-derivation).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "filtering_gtest.sh"
            # Honors --gtest_filter like real gtest: it has one real case (Suite.Case)
            # and emits it only when the filter selects it (absent or "*"); any other
            # filter (e.g. "src_compile") yields an empty result file.
            fake.write_text(
                "#!/bin/sh\n"
                'filter=""\n'
                'for arg in "$@"; do\n'
                '  case "$arg" in\n'
                '    --gtest_output=xml:*) out="${arg#--gtest_output=xml:}" ;;\n'
                '    --gtest_filter=*) filter="${arg#--gtest_filter=}" ;;\n'
                "  esac\n"
                "done\n"
                'mkdir -p "$(dirname "$out")"\n'
                'if [ -z "$filter" ] || [ "$filter" = "*" ] || [ "$filter" = "Suite.Case" ]; then\n'
                '  printf \'%s\' \'<testsuites tests="1" failures="0" skipped="0">'
                '<testsuite name="Suite" tests="1" failures="0" skipped="0">'
                '<testcase classname="Suite" name="Case" time="0"/></testsuite></testsuites>\' > "$out"\n'
                "else\n"
                '  printf \'%s\' \'<testsuites tests="0" failures="0" skipped="0"/>\' > "$out"\n'
                "fi\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            book = recipes.parse(
                f"""
targets:
  - id: src_compile
    binary: filtering_gtest.sh
    harness:
      kind: gtest
    test_run:
      cmd: [{str(fake)}]
"""
            )

            # Sanity: the whole suite is a real pass, so the errored result below is
            # caused by the no-match filter, not a broken binary.
            whole = gtest.run_target(root, book, "src_compile", run_id="r-all", tests=["*"])
            self.assertEqual(whole.overall, "passed")

            # The exact gear-up call: tests:["src_compile"] matches nothing -> errored.
            gated = gtest.run_target(root, book, "src_compile", run_id="r-gate", tests=["src_compile"])
            self.assertEqual(gated.overall, "errored")
            self.assertEqual(gated.failure, "no_tests_ran")

    def test_src_compile_runs_plain_and_sanitizer_profiles(self):
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
            # The code index is mandatory, so run_target now requires a working indexer toolchain;
            # the fake toolchain (JSON-AST oracle) keeps this hermetic without a real libclang.
            config = write_fake_toolchain(root, compile_database_path=root / "compile_commands.json")
            plain = gtest.run_target(
                root, book, "unit", run_id="plain", arbiter_bin=str(fake_arbiter), extractor_config=config
            )
            asan = gtest.run_target(
                root, book, "unit", run_id="asan", profiles=["asan"], arbiter_bin=str(fake_arbiter),
                extractor_config=config,
            )

            self.assertEqual(plain.overall, "passed")
            self.assertEqual(asan.overall, "passed")
            # The sanitizer profile applies its cflags to the build. Facts publication from the
            # build journal (CodeFactExtractor -> FileFactStore) is covered hermetically in test_pipeline.
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

    def test_isolation_per_suite_runs_each_suite_in_its_own_process(self):
        # harness isolation per_suite: run_target enumerates the suites and runs EACH in its own
        # process, then merges. The fake records every --gtest_filter it is invoked with, so we can
        # prove two separate suite-scoped runs happened (not one combined --gtest_filter=*).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_gtest.sh"
            fake.write_text(
                "#!/bin/sh\n"
                "for arg in \"$@\"; do\n"
                "  case \"$arg\" in\n"
                "    --gtest_list_tests) printf 'SuiteA.\\n  Case1\\nSuiteB.\\n  Case2\\n'; exit 0 ;;\n"
                "  esac\n"
                "done\n"
                "filter=''; out=''\n"
                "for arg in \"$@\"; do\n"
                "  case \"$arg\" in\n"
                "    --gtest_filter=*) filter=\"${arg#--gtest_filter=}\" ;;\n"
                "    --gtest_output=xml:*) out=\"${arg#--gtest_output=xml:}\" ;;\n"
                "  esac\n"
                "done\n"
                "printf '%s\\n' \"$filter\" >> filters.log\n"
                "mkdir -p \"$(dirname \"$out\")\"\n"
                "case \"$filter\" in\n"
                "  SuiteA.*) printf '<testsuites><testsuite name=\"SuiteA\"><testcase classname=\"SuiteA\" name=\"Case1\" time=\"0.001\"/></testsuite></testsuites>\\n' > \"$out\" ;;\n"
                "  SuiteB.*) printf '<testsuites><testsuite name=\"SuiteB\"><testcase classname=\"SuiteB\" name=\"Case2\" time=\"0.001\"><failure message=\"boom\">x</failure></testcase></testsuite></testsuites>\\n' > \"$out\" ;;\n"
                "esac\n"
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
      isolation: per_suite
    test_run:
      cmd: [{str(fake)}]
"""
            )

            result = gtest.run_target(root, book, "unit", run_id="iso", tests=["*"])

            # Merged across the two isolated suite runs: SuiteA passed, SuiteB failed.
            self.assertEqual(result.overall, "failed")
            self.assertEqual((result.passed, result.failed, result.skipped), (1, 1, 0))
            self.assertEqual(
                sorted((c.suite, c.name) for c in result.per_test),
                [("SuiteA", "Case1"), ("SuiteB", "Case2")],
            )
            # Each suite ran in its own process under a filter built from ITS enumerated cases —
            # the proof of isolation, and (per review #2) the actual case names, never a widening
            # "Suite.*" that would run tests the caller didn't ask for.
            filters = sorted((root / "filters.log").read_text(encoding="utf-8").split())
            self.assertEqual(filters, ["SuiteA.Case1", "SuiteB.Case2"])

    def test_unknown_isolation_value_rejected_at_register(self):
        # A typo'd isolation value is rejected when the recipe is PARSED/registered (review #6) —
        # cheaper and clearer than failing a run after a build is already spent, and it can never
        # block the build-booted boot gate.
        with self.assertRaises(recipes.RecipeError) as ctx:
            recipes.parse(
                """
targets:
  - id: unit
    binary: fake_gtest.sh
    harness:
      kind: gtest
      isolation: per_suit
    test_run:
      cmd: [./fake_gtest.sh]
"""
            )
        self.assertIn("isolation", str(ctx.exception))

    def test_parse_listing_and_per_suite_units_preserve_filter(self):
        # review #8: one parser for --gtest_list_tests output (shared by the boot counter and the
        # isolation unit-builder). review #2: per_suite builds units from the LISTED cases — which
        # gtest restricts to the caller's --gtest_filter — so a narrow request never widens to
        # "Suite.*" and run a case the caller didn't ask for.
        listing = "SuiteA.\n  Case1  # GetParam() = 1\nSuiteB.\n  Case2\n  Case3\n"
        parsed = gtest._parse_listing(listing)
        self.assertEqual(
            parsed,
            [("SuiteA.", ["SuiteA.Case1"]), ("SuiteB.", ["SuiteB.Case2", "SuiteB.Case3"])],
        )
        self.assertEqual(gtest._count_listed_tests(listing), 3)
        per_suite = [":".join(cases) for _suite, cases in parsed if cases]
        self.assertEqual(per_suite, ["SuiteA.Case1", "SuiteB.Case2:SuiteB.Case3"])
        per_test = [c for _suite, cases in parsed for c in cases]
        self.assertEqual(per_test, ["SuiteA.Case1", "SuiteB.Case2", "SuiteB.Case3"])


if __name__ == "__main__":
    unittest.main()

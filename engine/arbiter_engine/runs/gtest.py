"""GTest harness adapter."""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

from arbiter_engine import errors
from arbiter_engine.runs import guidance
from arbiter_engine.runs import recipes
from arbiter_engine.facts.extractor.code._shim import ExtractorConfig, InitError
from arbiter_engine.runs import runner
from arbiter_engine.runs.guidance import GuidanceEntry
from arbiter_engine.shared import pipeline

# The build-booted gate (recipe-derivation.md) runs src_compile under this no-match sentinel filter:
# it matches no real test (so the test run is a no-op needing no environment) AND signals run_target
# to capture the boot datum (`<binary:> --gtest_list_tests`). Must stay in sync with the predicate's
# `tests:` filter in the template.
BOOT_FILTER = "__arbiter_boot__"


@dataclass(frozen=True)
class PerTest:
    suite: str
    name: str
    occurrence: int
    status: str
    elapsed_ms: int
    message: str = ""


@dataclass(frozen=True)
class RunResult:
    run_id: str
    overall: str
    passed: int
    failed: int
    skipped: int
    per_test: Tuple[PerTest, ...] = ()
    guidance: Tuple[GuidanceEntry, ...] = ()
    facts: Optional[Mapping[str, object]] = None
    failure: Optional[str] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    # boot evidence: the process exit + listed-test count of `<binary:> --gtest_list_tests`
    # (a dedicated enumeration subprocess). None ⇒ no binary launched, so the boot clause
    # fails closed in the referee (internal/verify CompareRun).
    boot_exit_code: Optional[int] = None
    listed_tests: Optional[int] = None

    def to_json(self) -> dict:
        out = {
            "run_id": self.run_id,
            "overall": self.overall,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "per_test": [
                {
                    "suite": test.suite,
                    "name": test.name,
                    "occurrence": test.occurrence,
                    "status": test.status,
                    "elapsed_ms": test.elapsed_ms,
                    **({"message": test.message} if test.message else {}),
                }
                for test in self.per_test
            ],
        }
        if self.failure is not None:
            out["failure"] = self.failure
        # Carry the diagnostic tails to the envelope so a failed run's actual reason (e.g. the
        # indexer-toolchain message, a compiler error) reaches the referee/journal and the executor,
        # not just the terse failure code. Emitted only when non-empty so a clean run's shape is
        # unchanged.
        if self.stderr_tail:
            out["stderr_tail"] = self.stderr_tail
        if self.stdout_tail:
            out["stdout_tail"] = self.stdout_tail
        if self.guidance:
            out["guidance"] = [entry.to_json() for entry in self.guidance]
        if self.facts is not None:
            out["facts"] = dict(self.facts)
        # boot fields only when measured (non-None) so a run that never reached a binary keeps
        # its existing shape; the referee evaluates the boot clause off these when a predicate
        # asserts it (build-booted), and fails closed when they are absent.
        if self.boot_exit_code is not None:
            out["boot_exit_code"] = self.boot_exit_code
        if self.listed_tests is not None:
            out["listed_tests"] = self.listed_tests
        return out


def run_target(
    repo_root: Path | str,
    book: recipes.RecipeBook,
    target_id: str,
    *,
    run_id: str,
    tests: Sequence[str] = (),
    profiles: Sequence[str] = (),
    arbiter_bin: Optional[str] = None,
    fail_fast: bool = False,
    timeout_s: Optional[int] = None,
    extractor_config: Optional[ExtractorConfig] = None,
    facts_key_flags: Sequence[str] = (),
    facts_pool: Optional[int] = None,
) -> RunResult:
    root = Path(repo_root)
    arbiter_bin = runner.resolve_arbiter_bin(arbiter_bin)
    target = book.target(target_id)
    if target.harness.kind != "gtest":
        raise errors.harness_unavailable(target.harness.kind)
    if "test_run" not in target.stages:
        raise runner.RunnerError(f"target {target_id!r} has no test_run stage")
    try:
        workdir = runner.resolve_workdir(root, target)
    except runner.RunnerError as exc:
        return RunResult(
            run_id=run_id,
            overall="errored",
            passed=0,
            failed=0,
            skipped=0,
            failure="workdir_escape",
            stderr_tail=str(exc),
        )
    try:
        facts = _run_compile_stages(
            root,
            book,
            target,
            profiles=profiles,
            arbiter_bin=arbiter_bin,
            extractor_config=extractor_config,
            facts_key_flags=facts_key_flags,
            facts_pool=facts_pool,
        )
    except InitError as exc:
        if exc.code not in pipeline.TOOLCHAIN_FAILURE_CODES:
            # publish_after_build's contract is to re-raise only toolchain-class InitErrors; any
            # other InitError reaching here is not the mandatory-index hard stop, so don't mislabel
            # it as indexer_unavailable — let it propagate.
            raise
        # Mandatory-index hard stop: the indexer toolchain (clang/libclang) is unusable. The code
        # index is a must-have, so a match must not proceed on an unusable indexer — surface a typed
        # errored run instead of a green build with no facts behind it.
        return RunResult(
            run_id=run_id,
            overall="errored",
            passed=0,
            failed=0,
            skipped=0,
            failure="indexer_unavailable",
            stderr_tail=_indexer_unavailable_message(exc),
        )
    if facts.get("compile_failed"):
        return RunResult(
            run_id=run_id,
            overall="errored",
            passed=0,
            failed=0,
            skipped=0,
            facts=facts.get("facts"),
            failure=str(facts["compile_failed"]),
            stdout_tail=str(facts.get("stdout_tail", "")),
            stderr_tail=str(facts.get("stderr_tail", "")),
        )
    stage = target.stages["test_run"]
    env = runner._stage_env(os.environ, target, stage, book, profiles, "test_run", arbiter_bin)
    stage_timeout = timeout_s if timeout_s is not None else stage.timeout_s
    # boot datum, computed AFTER test_run.pre succeeds (below) — declared here so the pre-failure
    # return can carry it (None: a failed pre means no boot was attempted, fail-closed).
    boot_exit_code: Optional[int] = None
    listed_tests: Optional[int] = None
    # test_run.pre carries the runtime setup the test needs (start a service, generate
    # config/data, derive state) and must run BEFORE the test binary, sharing the stage's
    # env + workdir. The test path used to run only stage.cmd, so any test_run.pre was
    # silently dropped — a test that needed setup then failed as if its environment were
    # broken. A non-zero pre means the environment could not be established and no verdict is
    # obtainable, so it is `errored` (never a passable "failed"), the same as a build failure.
    for pre_command in stage.pre:
        pre_proc = runner._run_command(pre_command, workdir, env, stage_timeout)
        if pre_proc.exit_code != 0:
            return RunResult(
                run_id=run_id,
                overall="errored",
                passed=0,
                failed=0,
                skipped=0,
                facts=facts.get("facts"),
                failure="test_run_pre_failed",
                stdout_tail=pre_proc.stdout_tail,
                stderr_tail=pre_proc.stderr_tail,
                boot_exit_code=boot_exit_code,
                listed_tests=listed_tests,
            )
    # The build published facts and test_run.pre established the env; NOW capture the boot datum so
    # the build-booted gate can read it. The referee runs `<binary:> --gtest_list_tests` itself — a
    # DEDICATED subprocess, never the filtered test run whose exit gtest trivially zeroes — AFTER pre
    # so the binary loads in its established env. Stamped onto every post-build RunResult below: a
    # crash-on-boot binary lands on the exit_code / missing-result path (proc.exit_code != 0 is caught
    # before the no-match path), which would otherwise carry no boot datum.
    # ONLY the build-booted gate consumes boot evidence, and it is the only caller that runs under the
    # no-match BOOT_FILTER; gating the enumeration on that filter keeps candidate-proven / cover runs
    # (the cover step loops `run` over every binary) from paying an extra `--gtest_list_tests`
    # subprocess — and its worst-case timeout — on every run for a datum no predicate would read.
    if target.binary and list(tests) == [BOOT_FILTER]:
        boot_exit_code, listed_tests = _boot_enumerate(root / target.binary, workdir, env, stage_timeout)
    # Tool-handled test isolation. With `harness: {kind: gtest, isolation: per_suite|per_test}` the
    # recipe asks the runner to execute each suite (or case) in its OWN process and merge the
    # verdicts, so a test that only fails when run ALONGSIDE another (shared global/static state)
    # cannot make a working binary look broken — the model declares it once in the recipe instead of
    # hand-running tests one at a time. The compile stages already published facts above, so coverage
    # is untouched; this changes only HOW the cases run. Skipped under the no-match BOOT_FILTER (that
    # gate enumerates, it never runs cases).
    isolation_mode = target.harness.options.get("isolation")
    is_boot = list(tests) == [BOOT_FILTER]
    # bad_isolation is rejected at register/parse time now (recipes._parse_harness); this run-time
    # check is a defensive fallback, gated off the boot filter so a stray value can never block the
    # build-booted boot gate (which doesn't run cases under isolation anyway).
    if not is_boot and isolation_mode not in (None, "", "none", "per_suite", "per_test"):
        return RunResult(
            run_id=run_id,
            overall="errored",
            passed=0,
            failed=0,
            skipped=0,
            facts=facts.get("facts"),
            failure="bad_isolation",
            stderr_tail=f"unknown gtest isolation {isolation_mode!r}; use per_suite or per_test",
            boot_exit_code=boot_exit_code,
            listed_tests=listed_tests,
        )
    if isolation_mode in ("per_suite", "per_test") and not is_boot:
        return _run_isolated(
            root,
            target,
            stage,
            tests=tests,
            workdir=workdir,
            env=env,
            stage_timeout=stage_timeout,
            run_id=run_id,
            target_id=target_id,
            fail_fast=fail_fast,
            facts=facts,
            mode=isolation_mode,
        )
    run_dir = root / ".arbiter" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    xml_path = run_dir / f"{target_id}.xml"
    command = list(stage.cmd) + [f"--gtest_output=xml:{xml_path}"]
    if fail_fast:
        command.append("--gtest_fail_fast")
    if tests:
        command.append("--gtest_filter=" + ":".join(tests))
    # runner._run_command maps subprocess.TimeoutExpired to exit code 124 with
    # a tail message instead of letting the exception propagate.
    proc = runner._run_command(command, workdir, env, stage_timeout)
    # test_run.post is teardown (stop a service, clean up); run it best-effort AFTER the test
    # so a teardown failure never clobbers the test verdict the run just obtained.
    for post_command in stage.post:
        runner._run_command(post_command, workdir, env, stage_timeout)
    if proc.exit_code == 124 and not xml_path.exists():
        return RunResult(
            run_id=run_id,
            overall="errored",
            passed=0,
            failed=0,
            skipped=0,
            facts=facts.get("facts"),
            failure="timeout",
            stdout_tail=proc.stdout_tail,
            stderr_tail=proc.stderr_tail,
            boot_exit_code=boot_exit_code,
            listed_tests=listed_tests,
        )
    if not xml_path.exists():
        return RunResult(
            run_id=run_id,
            overall="errored",
            passed=0,
            failed=0,
            skipped=0,
            facts=facts.get("facts"),
            failure="missing_result_file",
            stdout_tail=proc.stdout_tail,
            stderr_tail=proc.stderr_tail,
            boot_exit_code=boot_exit_code,
            listed_tests=listed_tests,
        )
    try:
        result = parse_xml(xml_path, run_id=run_id)
    except ET.ParseError:
        return RunResult(
            run_id=run_id,
            overall="errored",
            passed=0,
            failed=0,
            skipped=0,
            facts=facts.get("facts"),
            failure="invalid_result_file",
            stdout_tail=proc.stdout_tail,
            stderr_tail=proc.stderr_tail,
            boot_exit_code=boot_exit_code,
            listed_tests=listed_tests,
        )
    if proc.exit_code != 0 and result.failed == 0:
        # The suite built and ran (results parsed) but the process still exited
        # non-zero with nothing marked failed: a sanitizer abort, a leak report,
        # or a crash at teardown. That is a genuine adverse verdict ("failed"),
        # NOT "errored" - the tests ran. "errored" is reserved above for the
        # cases where no verdict could be obtained at all (build broke, no
        # result file, timed out before completion).
        return RunResult(
            run_id=run_id,
            overall="failed",
            passed=result.passed,
            failed=result.failed,
            skipped=result.skipped,
            per_test=result.per_test,
            facts=facts.get("facts"),
            failure="exit_code",
            stdout_tail=proc.stdout_tail,
            stderr_tail=proc.stderr_tail,
            boot_exit_code=boot_exit_code,
            listed_tests=listed_tests,
        )
    if result.passed + result.failed + result.skipped == 0:
        # The filter matched no tests, so the recipe obtained no verdict at all -
        # gtest exits 0 on an empty run, which would otherwise read as "passed".
        # A green gate (expect overall=passed) must never be satisfied by zero
        # tests: a `tests` override naming a case that is a typo, or that was
        # never compiled into the recipe's binary, is no proof. "errored" keeps
        # it out of both gates, exactly like a build failure.
        return RunResult(
            run_id=run_id,
            overall="errored",
            passed=0,
            failed=0,
            skipped=0,
            facts=facts.get("facts"),
            failure="no_tests_ran",
            stdout_tail=proc.stdout_tail,
            stderr_tail=proc.stderr_tail,
            boot_exit_code=boot_exit_code,
            listed_tests=listed_tests,
        )
    if facts.get("facts") is not None:
        result = _with_facts(result, facts["facts"])
    if result.overall == "failed":
        result = _with_guidance(result, guidance.for_result(root, result))
    return replace(result, boot_exit_code=boot_exit_code, listed_tests=listed_tests)


def _errored(
    run_id: str,
    failure: Optional[str],
    facts_value: object,
    *,
    stdout_tail: str = "",
    stderr_tail: str = "",
    boot_exit_code: Optional[int] = None,
    listed_tests: Optional[int] = None,
) -> RunResult:
    """The canonical no-verdict RunResult (overall=errored, zero counts) — one shape so the
    single-process, isolated, and boot paths cannot drift on what an errored run looks like to the
    predicates that read it."""
    return RunResult(
        run_id=run_id,
        overall="errored",
        passed=0,
        failed=0,
        skipped=0,
        facts=facts_value,
        failure=failure,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        boot_exit_code=boot_exit_code,
        listed_tests=listed_tests,
    )


def _run_isolated(
    root: Path,
    target: recipes.Target,
    stage: recipes.Stage,
    *,
    tests: Sequence[str],
    workdir: Path,
    env: Mapping[str, str],
    stage_timeout: Optional[int],
    run_id: str,
    target_id: str,
    fail_fast: bool,
    facts: Mapping[str, object],
    mode: str,
) -> RunResult:
    """Run each gtest unit (suite for per_suite, case for per_test) in its OWN process and merge.

    Enumerate the units under the requested filter, run each on its own, and aggregate the verdicts
    into one RunResult shaped exactly like the single-process path (so the same predicates read it).
    A unit that produces no result row is treated as no verdict; a unit that crashed after running
    (non-zero exit, nothing marked failed) flags the merged run failed with `exit_code`. Facts and
    failure-guidance are attached once, at the end, as in the single-process path.
    """
    base = list(stage.cmd)
    units = _enumerate_units(base, tests, workdir, env, stage_timeout, mode)
    if units is None:
        # Enumeration itself failed (timeout / spawn error) -- NOT a real "no tests". Fall back to a
        # single-process run of the caller's filter, so an enumeration hiccup can't fail a proof the
        # cases would have passed. One unit = the whole requested filter (``*`` when unfiltered).
        units = [":".join(tests)] if tests else ["*"]
    run_dir = root / ".arbiter" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    total_passed = 0
    total_failed = 0
    total_skipped = 0
    per_test: list = []
    ran_any = False
    failure: Optional[str] = None
    stdout_tail = ""
    stderr_tail = ""
    for idx, unit in enumerate(units):
        xml_path = run_dir / f"{target_id}.u{idx}.xml"
        command = base + [f"--gtest_output=xml:{xml_path}"]
        if fail_fast:
            command.append("--gtest_fail_fast")
        command.append("--gtest_filter=" + unit)
        proc = runner._run_command(command, workdir, env, stage_timeout)
        result, unit_failure = _interpret_unit(proc, xml_path, run_id)
        if unit_failure is not None and failure is None:
            failure, stdout_tail, stderr_tail = unit_failure, proc.stdout_tail, proc.stderr_tail
        if result is not None:
            ran_any = True
            total_passed += result.passed
            total_failed += result.failed
            total_skipped += result.skipped
            per_test.extend(result.per_test)
        # fail_fast must bound total time ACROSS units, not just within one: stop at the first unit
        # that failed (a verdict with failures, or an adverse unit_failure). Without this, isolation
        # would defeat fail_fast -- worst case N full per-unit timeouts.
        if fail_fast and (unit_failure is not None or (result is not None and result.failed > 0)):
            break
    # teardown runs once, after every unit, like the single-process path's stage.post.
    for post_command in stage.post:
        runner._run_command(post_command, workdir, env, stage_timeout)
    if not ran_any:
        # No unit produced a verdict: an empty filter, or every unit errored. Mirror the
        # single-process no-verdict path — `errored` keeps it out of both green and red gates.
        return _errored(
            run_id,
            failure or "no_tests_ran",
            facts.get("facts"),
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    overall = "failed" if (total_failed > 0 or failure is not None) else "passed"
    result = RunResult(
        run_id=run_id,
        overall=overall,
        passed=total_passed,
        failed=total_failed,
        skipped=total_skipped,
        per_test=tuple(per_test),
        failure=failure,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )
    if facts.get("facts") is not None:
        result = _with_facts(result, facts["facts"])
    if result.overall == "failed":
        result = _with_guidance(result, guidance.for_result(root, result))
    return result


def _enumerate_units(
    base: Sequence[str],
    tests: Sequence[str],
    workdir: Path,
    env: Mapping[str, str],
    timeout_s: Optional[int],
    mode: str,
) -> Optional[list]:
    """List the unit filters to run in isolation, enumerated UNDER the caller's filter so a unit
    never adds a test the caller didn't ask for. per_test -> one filter per case; per_suite -> one
    filter per suite holding exactly that suite's enumerated cases (joined) -- NOT ``Suite.*``, which
    would widen a single-case request to the whole suite and silently change ``run`` semantics.
    Returns None when enumeration ITSELF failed (timeout / spawn error) so the caller can fall back
    to a single-process run rather than mistake the hiccup for "no tests". Uses a raw subprocess (not
    runner._run_command) because the FULL listing is needed -- _run_command tails stdout, which would
    truncate a large suite's enumeration."""
    command = list(base) + ["--gtest_list_tests"]
    if tests:
        command.append("--gtest_filter=" + ":".join(tests))
    try:
        proc = subprocess.run(
            command,
            cwd=str(workdir),
            env=dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    suites = _parse_listing(proc.stdout)
    if mode == "per_test":
        return [case for _suite, cases in suites for case in cases]
    return [":".join(cases) for _suite, cases in suites if cases]


def _parse_listing(listing: str) -> list:
    """Parse ``--gtest_list_tests`` output into ``[(suite_prefix, [full_case_name, ...]), ...]``.
    A flush-left line is a suite header ending in ``.``; an indented line is a case under it. gtest
    may append a ``# GetParam()=...`` comment to either, stripped here. The SINGLE parser for this
    format -- both the isolation unit-builder and the boot-gate case counter read it, so they cannot
    drift on parameterized-suite formatting."""
    suites: list = []
    prefix: Optional[str] = None
    cases: Optional[list] = None
    for line in listing.splitlines():
        if not line.strip():
            continue
        token = line.split("#", 1)[0].strip()
        if not token:
            continue
        if not line[0].isspace():
            prefix = token  # suite header, includes the trailing "."
            cases = []
            suites.append((prefix, cases))
        elif cases is not None:
            cases.append(prefix + token)  # full "Suite.Case"
    return suites


def _interpret_unit(proc, xml_path: Path, run_id: str) -> Tuple[Optional[RunResult], Optional[str]]:
    """Map one isolated unit's process + XML to (verdict, failure). The verdict is None when the
    unit yielded no result row (timed out, crashed before writing, or matched nothing); failure
    names the adverse reason when there is one (so the merge can flag the whole run)."""
    if not xml_path.exists():
        return None, ("timeout" if proc.exit_code == 124 else "missing_result_file")
    try:
        result = parse_xml(xml_path, run_id=run_id)
    except ET.ParseError:
        return None, "invalid_result_file"
    if result.passed + result.failed + result.skipped == 0:
        return None, None
    failure = "exit_code" if (proc.exit_code != 0 and result.failed == 0) else None
    return result, failure


def parse_xml(path: Path | str, *, run_id: str) -> RunResult:
    root = ET.parse(path).getroot()
    cases: list[PerTest] = []
    occurrences: dict[tuple[str, str], int] = {}
    passed = failed = skipped = 0
    for node in root.iter("testcase"):
        suite = node.attrib.get("classname") or node.attrib.get("class") or ""
        name = node.attrib.get("name") or ""
        key = (suite, name)
        occurrence = occurrences.get(key, 0) + 1
        occurrences[key] = occurrence
        elapsed_ms = _elapsed_ms(node.attrib.get("time", "0"))
        failure_node = _first_child(node, "failure")
        if failure_node is None:
            failure_node = _first_child(node, "error")
        skipped_node = _first_child(node, "skipped")
        if failure_node is not None:
            failed += 1
            status = "failed"
            message = failure_node.attrib.get("message") or (failure_node.text or "")
        elif skipped_node is not None:
            skipped += 1
            status = "skipped"
            message = skipped_node.attrib.get("message") or (skipped_node.text or "")
        elif _is_disabled(node):
            # A DISABLED test (gtest `status="notrun"`) never executed and carries no
            # <failure>/<error>/<skipped> child. Counting it as passed would inflate the
            # passed total and the per-test rows with a case that did not run, so it is
            # reported as skipped (the closest honest status).
            skipped += 1
            status = "skipped"
            message = ""
        else:
            passed += 1
            status = "passed"
            message = ""
        cases.append(
            PerTest(
                suite=suite,
                name=name,
                occurrence=occurrence,
                status=status,
                elapsed_ms=elapsed_ms,
                message=message,
            )
        )
    return RunResult(
        run_id=run_id,
        overall="failed" if failed else "passed",
        passed=passed,
        failed=failed,
        skipped=skipped,
        per_test=tuple(cases),
    )


def _first_child(node: ET.Element, name: str) -> Optional[ET.Element]:
    for child in node:
        if child.tag == name:
            return child
    return None


def _is_disabled(node: ET.Element) -> bool:
    """A gtest DISABLED test case that did not run.

    gtest stamps every ``<testcase>`` with ``status``: ``"run"`` for one that
    executed and ``"notrun"`` for a DISABLED case that was skipped, which carries
    no ``<failure>``/``<error>``/``<skipped>`` child. ``"notrun"`` is the authoritative
    signal; the ``DISABLED_`` name prefix alone is NOT used, because a DISABLED-named
    test that actually ran (e.g. under ``--gtest_also_run_disabled_tests``) reports
    ``status="run"`` and must stay a real pass/fail, not be hidden as skipped.
    """
    return node.attrib.get("status") == "notrun"


def _elapsed_ms(raw: str) -> int:
    try:
        return int(round(float(raw) * 1000))
    except ValueError:
        return 0


def _with_guidance(result: RunResult, entries: Tuple[GuidanceEntry, ...]) -> RunResult:
    return RunResult(
        run_id=result.run_id,
        overall=result.overall,
        passed=result.passed,
        failed=result.failed,
        skipped=result.skipped,
        per_test=result.per_test,
        guidance=entries,
        facts=result.facts,
        failure=result.failure,
        stdout_tail=result.stdout_tail,
        stderr_tail=result.stderr_tail,
    )


def _with_facts(result: RunResult, facts: Mapping[str, object]) -> RunResult:
    return RunResult(
        run_id=result.run_id,
        overall=result.overall,
        passed=result.passed,
        failed=result.failed,
        skipped=result.skipped,
        per_test=result.per_test,
        guidance=result.guidance,
        facts=facts,
        failure=result.failure,
        stdout_tail=result.stdout_tail,
        stderr_tail=result.stderr_tail,
    )


def _indexer_unavailable_message(exc: InitError) -> str:
    detail = ", ".join(f"{key}={value}" for key, value in sorted(exc.details.items()) if value != "")
    suffix = f" ({detail})" if detail else ""
    return (
        f"indexer toolchain unavailable [{exc.code}]: {exc.message}{suffix}. "
        "The code index is mandatory — install a matching clang/libclang, or pin one for the "
        "indexer via .arbiter/config.yml facts.toolchain (clang / libclang / clang_args)."
    )


def _boot_enumerate(
    binary: Path,
    workdir: Path,
    env: Mapping[str, str],
    timeout_s: Optional[int],
) -> Tuple[Optional[int], Optional[int]]:
    """Prove the cc-built binary BOOTS by running `<binary> --gtest_list_tests`.

    Returns (exit_code, listed_tests). The process exit code proves the binary links and
    loads its shared libraries; the listed-test count proves it is a genuine gtest binary
    that enumerates >=1 case — together they close the cmd:[true]/echo cheat (which exits 0
    but lists nothing) WITHOUT requiring any test to pass (that is the prove step). Returns
    (None, None) when `binary:` does not resolve to a file, so the boot verdict fails closed.
    Enumeration only loads the binary (static init + TEST registration); it never runs a test
    body, so it needs no runtime environment of its own.
    """
    if not binary.is_file():
        return None, None
    try:
        proc = subprocess.run(
            [str(binary), "--gtest_list_tests"],
            cwd=str(workdir),
            env=dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, 0
    except OSError:
        return 127, 0
    return proc.returncode, _count_listed_tests(proc.stdout)


def _count_listed_tests(listing: str) -> int:
    """Count cases in `--gtest_list_tests` output via the shared listing parser (so it cannot drift
    from the isolation unit-builder). A non-gtest `true`/`echo` prints nothing, yielding 0."""
    return sum(len(cases) for _suite, cases in _parse_listing(listing))


def _run_compile_stages(
    root: Path,
    book: recipes.RecipeBook,
    target: recipes.Target,
    *,
    profiles: Sequence[str],
    arbiter_bin: str,
    extractor_config: Optional[ExtractorConfig],
    facts_key_flags: Sequence[str],
    facts_pool: Optional[int],
) -> dict[str, object]:
    facts: Optional[Mapping[str, object]] = None
    for stage_name in ("src_compile", "test_compile"):
        if stage_name not in target.stages:
            continue
        result = runner.run_stage(root, book, target.id, stage_name, profiles=profiles, arbiter_bin=arbiter_bin)
        if stage_name == "src_compile":
            facts = _publish_compile_facts(
                root,
                book,
                target,
                stage_name,
                build_succeeded=result.exit_code == 0,
                extractor_config=extractor_config,
                facts_key_flags=facts_key_flags,
                facts_pool=facts_pool,
                profiles=profiles,
            )
        if result.exit_code != 0:
            return {
                "compile_failed": stage_name,
                "facts": facts,
                "stdout_tail": result.stdout_tail,
                "stderr_tail": result.stderr_tail,
            }
    return {"facts": facts}


def _publish_compile_facts(
    root: Path,
    book: recipes.RecipeBook,
    target: recipes.Target,
    stage_name: str,
    *,
    build_succeeded: bool,
    extractor_config: Optional[ExtractorConfig],
    facts_key_flags: Sequence[str],
    facts_pool: Optional[int],
    profiles: Sequence[str],
) -> Optional[Mapping[str, object]]:
    if book.compile_db is None:
        # A recipe with no top-level `compile_db:` section can build and run, but can never
        # publish facts: the extractor has no compile-command source to index. Returning a
        # silent None left a facts-gated step (gear-up-published, tests-enumerated) failing with
        # an opaque journal_miss and no way for the author to learn what was missing. Surface a
        # typed, not-published result naming the absent section instead.
        return pipeline.PipelineResult(
            published=False,
            snapshot_id=None,
            files=0,
            warnings=[{
                "kind": "no_compile_db",
                "message": "facts cannot publish: this recipe has no top-level `compile_db:` "
                "section. Add one, as a sibling of `targets:`, with `path:` set to the build's "
                "compile_commands.json (configure cmake with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON), "
                "e.g.\ncompile_db:\n  path: build/compile_commands.json",
            }],
            extract_ms=0,
            hidden_ms=0,
            tail_ms=0,
        ).to_json()
    journals = _compile_journals(root, target, stage_name)
    # The target's source TUs, for the cache-independent index fallback: a cmake no-op
    # (already built, or built as another target's dependency) leaves the cc journal empty,
    # so publish recovers these files' real commands from the build's compile_commands.json
    # rather than indexing nothing — a built binary still indexes without a recompile.
    recover_sources = tuple(
        path
        for pattern in (target.sources or ())
        for path in root.glob(pattern)
        if path.is_file()
    )
    if not recover_sources:
        # Batch-registered cover targets often declare no `sources`. Default the
        # cache-independent fallback to the project's DECLARED test files (the AST scan,
        # vendored third-party excluded) so a built binary still indexes the suite — the
        # journal stays authoritative whenever the build actually compiled something.
        from arbiter_engine.runs import discovery as _discovery

        recover_sources = tuple(
            {
                root / candidate.file
                for candidate in _discovery.discover_declared_tests(root)
                if _discovery._in_project_scope(candidate.file)
            }
        )
    if not any(path.exists() for path in journals) and not recover_sources:
        return pipeline.PipelineResult(
            published=False,
            snapshot_id=None,
            files=0,
            warnings=[{"kind": "journal_miss", "message": "compile journal was not produced"}],
            extract_ms=0,
            hidden_ms=0,
            tail_ms=0,
        ).to_json()
    result = pipeline.publish_after_build(
        root,
        journals,
        root / book.compile_db.path,
        build_succeeded=build_succeeded,
        key_flags=facts_key_flags,
        pool=facts_pool,
        profile="+".join(profiles) if profiles else "default",
        extractor_config=extractor_config,
        recover_sources=recover_sources,
    )
    payload = result.to_json()
    # A `binary:` that does not resolve after a green build silently disables the build cache
    # (build_cache.lookup requires the file): every subsequent run then resets the journal and
    # recompiles incrementally, publishing a partial snapshot that clobbers the complete one —
    # which reads downstream as a passing build whose tests-enumerated goal never satisfies. Name
    # it so the author points `binary:` at where the build actually writes the test binary.
    if build_succeeded and target.binary and not (root / target.binary).exists():
        warnings = list(payload.get("warnings") or [])
        warnings.append({
            "kind": "binary_not_found",
            "message": f"the recipe's `binary:` path {target.binary!r} does not exist after a green "
            f"build, so the build cache is disabled and each run recompiles incrementally and "
            f"publishes an INCOMPLETE facts snapshot. Set `binary:` to the test binary's path "
            f"relative to the repo root (where the build writes it, typically under build/, "
            f"e.g. build/{Path(target.binary).name}).",
        })
        payload["warnings"] = warnings
    return payload


def _compile_journals(root: Path, target: recipes.Target, stage_name: str) -> Tuple[Path, ...]:
    build_id = f"{target.id}-{stage_name}"
    rel = Path(".arbiter") / "facts" / "run" / f"compile-journal.{build_id}.jsonl"
    workdir = root / target.workdir
    paths = [root / rel, workdir / rel]
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return tuple(unique)

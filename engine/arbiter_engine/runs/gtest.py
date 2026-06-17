"""GTest harness adapter."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

from arbiter_engine import errors
from arbiter_engine.runs import guidance
from arbiter_engine.runs import recipes
from arbiter_engine.facts.extractor.code._shim import ExtractorConfig
from arbiter_engine.runs import runner
from arbiter_engine.runs.guidance import GuidanceEntry
from arbiter_engine.shared import pipeline


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
        if self.guidance:
            out["guidance"] = [entry.to_json() for entry in self.guidance]
        if self.facts is not None:
            out["facts"] = dict(self.facts)
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
    run_dir = root / ".arbiter" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    xml_path = run_dir / f"{target_id}.xml"
    command = list(stage.cmd) + [f"--gtest_output=xml:{xml_path}"]
    if fail_fast:
        command.append("--gtest_fail_fast")
    if tests:
        command.append("--gtest_filter=" + ":".join(tests))
    env = runner._stage_env(os.environ, target, stage, book, profiles, "test_run", arbiter_bin)
    stage_timeout = timeout_s if timeout_s is not None else stage.timeout_s
    # runner._run_command maps subprocess.TimeoutExpired to exit code 124 with
    # a tail message instead of letting the exception propagate.
    proc = runner._run_command(command, workdir, env, stage_timeout)
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
        )
    if facts.get("facts") is not None:
        result = _with_facts(result, facts["facts"])
    if result.overall == "failed":
        return _with_guidance(result, guidance.for_result(root, result))
    return result


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
    if not any(path.exists() for path in journals):
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

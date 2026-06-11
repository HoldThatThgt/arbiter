"""GTest harness adapter."""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

from arbiter_engine import errors
from arbiter_engine.runs import recipes
from arbiter_engine.runs import runner


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
        return out


def run_target(
    repo_root: Path | str,
    book: recipes.RecipeBook,
    target_id: str,
    *,
    run_id: str,
    tests: Sequence[str] = (),
    profiles: Sequence[str] = (),
    arbiter_bin: str = "arbiter",
) -> RunResult:
    root = Path(repo_root)
    target = book.target(target_id)
    if target.harness.kind != "gtest":
        raise errors.harness_unavailable(target.harness.kind)
    if "test_run" not in target.stages:
        raise runner.RunnerError(f"target {target_id!r} has no test_run stage")
    stage = target.stages["test_run"]
    run_dir = root / ".arbiter" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    xml_path = run_dir / f"{target_id}.xml"
    command = list(stage.cmd) + [f"--gtest_output=xml:{xml_path}"]
    if tests:
        command.append("--gtest_filter=" + ":".join(tests))
    workdir = root / target.workdir
    env = runner._stage_env(os.environ, target, stage, book, profiles, "test_run", arbiter_bin)
    proc = subprocess.run(
        command,
        cwd=str(workdir),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=stage.timeout_s,
        check=False,
    )
    if not xml_path.exists():
        return RunResult(
            run_id=run_id,
            overall="failed",
            passed=0,
            failed=0,
            skipped=0,
            failure="missing_result_file",
            stdout_tail=runner._tail(proc.stdout),
            stderr_tail=runner._tail(proc.stderr),
        )
    try:
        result = parse_xml(xml_path, run_id=run_id)
    except ET.ParseError:
        return RunResult(
            run_id=run_id,
            overall="failed",
            passed=0,
            failed=0,
            skipped=0,
            failure="invalid_result_file",
            stdout_tail=runner._tail(proc.stdout),
            stderr_tail=runner._tail(proc.stderr),
        )
    if proc.returncode != 0 and result.failed == 0:
        return RunResult(
            run_id=run_id,
            overall="failed",
            passed=result.passed,
            failed=result.failed,
            skipped=result.skipped,
            per_test=result.per_test,
            failure="exit_code",
            stdout_tail=runner._tail(proc.stdout),
            stderr_tail=runner._tail(proc.stderr),
        )
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

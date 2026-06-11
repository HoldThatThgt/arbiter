"""Recipe stage runner."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from arbiter_engine.runs import recipes
from arbiter_engine.shared import locks


TAIL_BYTES = 4096
COMPILE_STAGES = {"src_compile", "test_compile"}
SECRET_NAME = re.compile(r"(^|_)(SECRET|TOKEN|PASSWORD|API_KEY|ACCESS_KEY|PRIVATE_KEY)(_|$)")


class RunnerError(ValueError):
    pass


@dataclass(frozen=True)
class StageResult:
    target_id: str
    stage: str
    exit_code: int
    stdout_tail: str = ""
    stderr_tail: str = ""


def run_stage(
    repo_root: Path | str,
    book: recipes.RecipeBook,
    target_id: str,
    stage: str,
    *,
    profiles: Sequence[str] = (),
    arbiter_bin: str = "arbiter",
    lock_timeout_s: float = 30.0,
) -> StageResult:
    root = Path(repo_root)
    target = book.target(target_id)
    if stage not in target.stages:
        raise RunnerError(f"target {target_id!r} has no stage {stage!r}")
    workdir = root / target.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    stage_spec = target.stages[stage]
    env = _stage_env(os.environ, target, stage_spec, book, profiles, stage, arbiter_bin)

    with locks.acquire(root, [locks.build_lock(workdir)], timeout_s=lock_timeout_s):
        for command in stage_spec.pre:
            result = _run_command(command, workdir, env, stage_spec.timeout_s)
            if result.exit_code != 0:
                return _with_identity(result, target_id, stage)
        result = _run_command(stage_spec.cmd, workdir, env, stage_spec.timeout_s)
        if result.exit_code != 0:
            return _with_identity(result, target_id, stage)
        for command in stage_spec.post:
            result = _run_command(command, workdir, env, stage_spec.timeout_s)
            if result.exit_code != 0:
                return _with_identity(result, target_id, stage)
    return _with_identity(result, target_id, stage)


def _stage_env(
    base: Mapping[str, str],
    target: recipes.Target,
    stage: recipes.Stage,
    book: recipes.RecipeBook,
    profiles: Sequence[str],
    stage_name: str,
    arbiter_bin: str,
) -> dict[str, str]:
    env = dict(base)
    _merge_env(env, target.env)
    _merge_env(env, stage.env)
    if stage_name in COMPILE_STAGES:
        for profile_name in profiles:
            try:
                profile = book.profiles[profile_name]
            except KeyError as exc:
                raise RunnerError(f"unknown profile {profile_name!r}") from exc
            _merge_env(env, profile.env)
            _append_flags(env, "CFLAGS", profile.cflags_append)
            _append_flags(env, "CXXFLAGS", profile.cxxflags_append)
            _append_flags(env, "LDFLAGS", profile.ldflags_append)
        _inject_cc(env, "CC", arbiter_bin, "cc")
        if "CXX" in env:
            _inject_cc(env, "CXX", arbiter_bin, "c++")
    return env


def _merge_env(env: dict[str, str], values: Mapping[str, str]) -> None:
    for key, value in values.items():
        if SECRET_NAME.search(key):
            raise RunnerError(f"secret-shaped env name {key!r} is not allowed")
        env[key] = value


def _append_flags(env: dict[str, str], name: str, flags: Sequence[str]) -> None:
    if not flags:
        return
    existing = env.get(name, "")
    suffix = " ".join(flags)
    env[name] = f"{existing} {suffix}".strip()


def _inject_cc(env: dict[str, str], name: str, arbiter_bin: str, default: str) -> None:
    real = env.get(name, default)
    env[name] = f"{arbiter_bin} cc -- {real}"


def _run_command(
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: int | None,
) -> StageResult:
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(cwd),
            env=dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
        return StageResult("", "", proc.returncode, _tail(proc.stdout), _tail(proc.stderr))
    except subprocess.TimeoutExpired as exc:
        return StageResult("", "", 124, _tail(exc.stdout or ""), _tail(exc.stderr or "timeout"))
    except OSError as exc:
        return StageResult("", "", 127, "", str(exc))


def _with_identity(result: StageResult, target_id: str, stage: str) -> StageResult:
    return StageResult(target_id, stage, result.exit_code, result.stdout_tail, result.stderr_tail)


def _tail(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    data = value.encode("utf-8")[-TAIL_BYTES:]
    return data.decode("utf-8", "replace")

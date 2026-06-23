"""Recipe stage runner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from arbiter_engine.runs import build_cache
from arbiter_engine.runs import recipes
from arbiter_engine.shared import locks


TAIL_BYTES = 4096
COMPILE_STAGES = {"src_compile", "test_compile"}
STATE_DB_REL = Path(".arbiter") / "runs" / "state.sqlite"
SECRET_NAME = re.compile(r"(^|_)(SECRET|TOKEN|PASSWORD|API_KEY|ACCESS_KEY|PRIVATE_KEY)(_|$)")
DEFAULT_ARBITER_BIN = "arbiter"


class RunnerError(ValueError):
    pass


@dataclass(frozen=True)
class StageResult:
    target_id: str
    stage: str
    exit_code: int
    stdout_tail: str = ""
    stderr_tail: str = ""


def resolve_arbiter_bin(arbiter_bin: Optional[str] = None) -> str:
    """Resolve the arbiter binary: explicit argument, then $ARBITER_BIN, then PATH lookup."""
    if arbiter_bin:
        return arbiter_bin
    return os.environ.get("ARBITER_BIN") or DEFAULT_ARBITER_BIN


def resolve_workdir(repo_root: Path | str, target: recipes.Target) -> Path:
    """Resolve a target workdir, rejecting paths that escape the repo root."""
    root = Path(repo_root).resolve()
    workdir = (root / target.workdir).resolve()
    if workdir != root and root not in workdir.parents:
        raise RunnerError(
            f"target {target.id!r} workdir {target.workdir!r} escapes the repo root"
        )
    return workdir


def run_stage(
    repo_root: Path | str,
    book: recipes.RecipeBook,
    target_id: str,
    stage: str,
    *,
    profiles: Sequence[str] = (),
    arbiter_bin: Optional[str] = None,
    lock_timeout_s: float = 30.0,
) -> StageResult:
    root = Path(repo_root)
    arbiter_bin = resolve_arbiter_bin(arbiter_bin)
    target = book.target(target_id)
    if stage not in target.stages:
        raise RunnerError(f"target {target_id!r} has no stage {stage!r}")
    workdir = resolve_workdir(root, target)
    workdir.mkdir(parents=True, exist_ok=True)
    stage_spec = target.stages[stage]
    env = _stage_env(os.environ, target, stage_spec, book, profiles, stage, arbiter_bin)

    cache_eligible = stage in COMPILE_STAGES and target.binary is not None
    db_path = root / STATE_DB_REL
    cache_key = (
        _cache_key(target, stage_spec, stage, profiles, book) if cache_eligible else ""
    )

    with locks.acquire(root, [locks.build_lock(workdir)], timeout_s=lock_timeout_s):
        # A census-validated cache hit (matching stage key, clean census over the
        # recipe's sources globs, and the expected binary still present) lets us
        # skip the compile entirely and reuse the prior binary. Recipes without
        # `sources:` never hit cross-process — build_cache.lookup enforces that.
        if cache_eligible and build_cache.lookup(
            db_path, root, key=cache_key, sources=target.sources
        ) is not None:
            return StageResult(target_id, stage, 0)
        if stage in COMPILE_STAGES:
            _reset_compile_journal(root, workdir, env["ARBITER_BUILD_ID"])
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
        # The compile (and any pre/post) succeeded: record the census-bound binary
        # so an identical future build can be skipped. store() writes an empty
        # digest when there are no sources, which lookup() never treats as a hit.
        if cache_eligible:
            build_cache.store(
                db_path,
                root,
                key=cache_key,
                binary=target.binary,
                sources=target.sources,
            )
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
        env.setdefault("ARBITER_BUILD_ID", f"{target.id}-{stage_name}")
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


def _inject_cc(env: dict[str, str], name: str, arbiter_bin: Optional[str], default: str) -> None:
    real = env.get(name, default)
    env[name] = f"{resolve_arbiter_bin(arbiter_bin)} cc -- {real}"


def _cache_key(
    target: recipes.Target,
    stage: recipes.Stage,
    stage_name: str,
    profiles: Sequence[str],
    book: recipes.RecipeBook,
) -> str:
    """Stable build-cache key for a compile stage.

    The key binds every recipe-controlled input that determines the produced
    binary, not just identity. ADR-0005 requires keying on "full flags +
    profile", so a hit must be impossible unless the compile would re-run the
    exact same command with the exact same flags/env:

      * target id, stage, and the active profile overlay (profiles change
        CFLAGS/CXXFLAGS/LDFLAGS, so a debug build must never satisfy an asan
        lookup);
      * the stage's resolved compile command and pre/post argv (changing
        src_compile.cmd must miss — it produces a different binary);
      * the effective compile/link flags and env that reach the compiler:
        target.env, stage.env, and each applied profile's env and
        cflags/cxxflags/ldflags appends (CFLAGS settable via env, e.g. -O0→-O2,
        must miss).

    The source census (build_cache.lookup) excludes .arbiter/ and only sees the
    `sources:` globs, so without this binding a recipe/flag edit is invisible
    and the stale binary is served as fresh. Profiles are applied as an
    unordered set by _stage_env, so the profile contribution is sorted to stay
    order-insensitive; the digest is a deterministic sha256 over canonical JSON.
    """
    profile_key = "+".join(sorted(profiles)) if profiles else "default"
    payload = {
        "target_id": target.id,
        "stage": stage_name,
        "profiles": sorted(profiles),
        # stage.to_json() emits cmd/pre/post/env/timeout_s deterministically.
        "stage_spec": stage.to_json(),
        "target_env": dict(sorted(target.env.items())),
        # Bind each applied profile's *contents* (flags + env), not just its
        # name: under recipe-derivation book drift the same name may be redefined
        # with weaker flags between the proving run and a re-run.
        "profile_overlay": [
            _profile_digest_input(book, name) for name in sorted(profiles)
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{profile_key}:{target.id}:{stage_name}:{digest}"


def _profile_digest_input(book: recipes.RecipeBook, name: str) -> dict:
    """Canonical, JSON-serializable view of a profile's compile contribution.

    An unknown profile name is left as a bare marker rather than raising: the
    runner's own _stage_env raises RunnerError for unknown profiles when it
    actually builds, and the cache key must not crash before that check runs.
    """
    profile = book.profiles.get(name)
    if profile is None:
        return {"name": name, "unknown": True}
    return {"name": name, **profile.to_json()}


def _reset_compile_journal(root: Path, workdir: Path, build_id: str) -> None:
    """Remove stale compile journals for this build id before the stage runs.

    The journal is owned by the build identified by ARBITER_BUILD_ID; records
    left over from previous builds (including old miss markers) must not leak
    into publish_after_build.
    """
    rel = Path(".arbiter") / "facts" / "run" / f"compile-journal.{build_id}.jsonl"
    seen: set[Path] = set()
    for base in (root, workdir):
        path = base / rel
        if path in seen:
            continue
        seen.add(path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


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

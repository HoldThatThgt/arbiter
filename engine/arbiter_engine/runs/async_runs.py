"""Async run worker storage for arbiter/startRun and arbiter/runStatus."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from arbiter_engine.errors import RPCError
from arbiter_engine.runs import gtest
from arbiter_engine.runs import recipes
from arbiter_engine.runs import state as run_state


MAX_TIMEOUT_S = 3600
DEFAULT_TIMEOUT_S = 600
_SPEC_KEYS = frozenset(
    {
        "expect",
        "kind",
        "options",
        "recipe",
        "result",
        "sleep_ms",
        "tests",
        "timeout_s",
    }
)


def start_run(repo: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    checked = _validate_spec(spec)
    run_id = uuid.uuid4().hex
    db_path = _db_path(repo)
    _init_db(db_path)
    _insert_run(db_path, run_id, checked)
    _spawn_worker(db_path, run_id, checked)
    return {"run_id": run_id, "state": "running"}


def run_status(repo: Path, run_id: str) -> dict[str, Any]:
    if not run_id:
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "run_id"})

    db_path = _db_path(repo)
    _init_db(db_path)
    with contextlib.closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
        row = conn.execute(
            "SELECT state, result_json, worker_pid FROM async_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    if row is None:
        return {"run_id": run_id, "state": "unknown"}

    state, result_json, worker_pid = row
    if state == "running" and worker_pid is not None and not _pid_alive(int(worker_pid)):
        # The worker died without recording a result; fail the row so callers
        # are not wedged on a run that can never finish.
        lost = {"overall": "failed", "failure": "worker_lost"}
        if _finish_if_running(db_path, run_id, "failed", lost):
            return {"run_id": run_id, "state": "failed", "result": lost}
        # The worker won the race and finished after our read; re-read the row.
        with contextlib.closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
            row = conn.execute(
                "SELECT state, result_json, worker_pid FROM async_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return {"run_id": run_id, "state": "unknown"}
        state, result_json, worker_pid = row

    response: dict[str, Any] = {"run_id": run_id, "state": state}
    if result_json:
        response["result"] = json.loads(result_json)
    return response


def _validate_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "spec"})

    unknown = sorted(set(spec) - _SPEC_KEYS)
    if unknown:
        raise RPCError(
            -32602,
            "invalid params",
            {"kind": "invalid_params", "bad_spec_params": unknown},
        )

    kind = spec.get("kind", "stub")
    if not isinstance(kind, str) or kind not in {"stub", "run"}:
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "kind"})

    timeout_s = spec.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(timeout_s, int) or isinstance(timeout_s, bool):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "timeout_s"})
    if timeout_s < 1 or timeout_s > MAX_TIMEOUT_S:
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "timeout_s"})

    sleep_ms = spec.get("sleep_ms", 0)
    if not isinstance(sleep_ms, int) or isinstance(sleep_ms, bool) or sleep_ms < 0:
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "sleep_ms"})

    result = spec.get("result", {"overall": "passed"})
    if not isinstance(result, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "result"})
    recipe = spec.get("recipe", "")
    if recipe is not None and not isinstance(recipe, str):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "recipe"})
    if kind == "run" and (not isinstance(recipe, str) or not recipe.strip()):
        # kind=="run" specs REQUIRE a non-empty recipe; stub execution must
        # never be reachable from a recipe-less run spec.
        raise RPCError(
            -32602,
            "invalid params",
            {
                "kind": "invalid_params",
                "field": "recipe",
                "detail": "kind=\"run\" requires a non-empty recipe",
            },
        )
    tests = spec.get("tests", [])
    if not isinstance(tests, list) or not all(isinstance(item, str) for item in tests):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "tests"})
    options = spec.get("options", {})
    if not isinstance(options, dict):
        raise RPCError(-32602, "invalid params", {"kind": "invalid_params", "field": "options"})

    checked = dict(spec)
    checked["kind"] = kind
    checked["timeout_s"] = timeout_s
    checked["sleep_ms"] = sleep_ms
    checked["result"] = dict(result)
    return checked


def _db_path(repo: Path) -> Path:
    return repo / ".arbiter" / "runs" / "state.sqlite"


def _init_db(path: Path) -> None:
    run_state.init(path)


def _insert_run(path: Path, run_id: str, spec: Mapping[str, Any]) -> None:
    now = time.time()
    with contextlib.closing(sqlite3.connect(str(path), timeout=30)) as conn:
        conn.execute(
            """
            INSERT INTO async_runs
                (run_id, state, spec_json, result_json, worker_pid, started_at, updated_at)
            VALUES (?, 'running', ?, NULL, NULL, ?, ?)
            """,
            (run_id, json.dumps(spec, sort_keys=True, separators=(",", ":")), now, now),
        )
        conn.commit()


def _spawn_worker(path: Path, run_id: str, spec: Mapping[str, Any]) -> None:
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            second = os.fork()
            if second != 0:
                os._exit(0)
            spec_json = json.dumps(dict(spec), sort_keys=True, separators=(",", ":"))
            os.execve(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "arbiter_engine.runs.async_runs",
                    "worker",
                    str(path),
                    run_id,
                    spec_json,
                ],
                _worker_env(),
            )
        except BaseException as exc:
            _log_worker_error(path, run_id, exc)
            _finish(path, run_id, "failed", {"overall": "failed", "failure": type(exc).__name__})
            os._exit(1)

    _, status = os.waitpid(pid, 0)
    if status != 0:
        _finish(path, run_id, "failed", {"overall": "failed", "failure": "worker_spawn"})


def _worker_main(path: Path, run_id: str, spec: Mapping[str, Any]) -> None:
    _record_worker(path, run_id, os.getpid())
    try:
        _arm_deadline(spec)
        result = _run_payload(path, run_id, spec)
        signal.alarm(0)
        _finish(path, run_id, "completed", result)
        os._exit(0)
    except _PayloadTimeout:
        signal.alarm(0)
        _finish(path, run_id, "failed", {"overall": "failed", "failure": "timeout"})
        os._exit(0)
    except BaseException as exc:
        signal.alarm(0)
        _log_worker_error(path, run_id, exc)
        _finish(path, run_id, "failed", {"overall": "failed", "failure": type(exc).__name__})
        os._exit(1)


def _arm_deadline(spec: Mapping[str, Any]) -> None:
    """Arm a SIGALRM deadline so the spec timeout_s applies to every payload kind."""
    timeout_s = spec.get("timeout_s")
    if isinstance(timeout_s, int) and not isinstance(timeout_s, bool) and timeout_s > 0:
        signal.signal(signal.SIGALRM, _deadline_expired)
        signal.alarm(timeout_s)


def _deadline_expired(signum: int, frame: Any) -> None:
    del signum, frame
    raise _PayloadTimeout()


def _run_payload(path: Path, run_id: str, spec: Mapping[str, Any]) -> Mapping[str, Any]:
    if spec.get("kind") == "run":
        recipe = spec.get("recipe")
        if not isinstance(recipe, str) or not recipe.strip():
            # Defense in depth: _validate_spec rejects these at startRun time.
            # A kind=="run" spec must never fall through to the stub path.
            raise ValueError('kind="run" spec requires a non-empty recipe')
        return _run_recipe(path, run_id, spec)
    sleep_s = float(spec["sleep_ms"]) / 1000.0
    command = "import time; time.sleep(%r)" % sleep_s
    proc = subprocess.Popen(
        [sys.executable, "-c", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    try:
        proc.wait(timeout=float(spec["timeout_s"]))
    except subprocess.TimeoutExpired:
        _kill_process_group(proc.pid)
        proc.wait()
        raise _PayloadTimeout()

    if proc.returncode != 0:
        return {"overall": "failed", "failure": "exit_code", "exit_code": proc.returncode}
    return dict(spec["result"])


def _run_recipe(path: Path, run_id: str, spec: Mapping[str, Any]) -> Mapping[str, Any]:
    repo = _repo_from_db(path)
    book = recipes.load(repo / ".arbiter" / "recipes.yaml")
    options = spec.get("options", {})
    profiles = options.get("profiles", []) if isinstance(options, Mapping) else []
    result = gtest.run_target(
        repo,
        book,
        str(spec["recipe"]),
        run_id=run_id,
        tests=spec.get("tests", []),
        profiles=profiles,
    )
    payload = result.to_json()
    payload["isError"] = False
    return payload


def _repo_from_db(path: Path) -> Path:
    return path.parent.parent.parent


def _record_worker(path: Path, run_id: str, pid: int) -> None:
    with contextlib.closing(sqlite3.connect(str(path), timeout=30)) as conn:
        conn.execute(
            "UPDATE async_runs SET worker_pid = ?, updated_at = ? WHERE run_id = ?",
            (pid, time.time(), run_id),
        )
        conn.commit()


def _finish(path: Path, run_id: str, state: str, result: Mapping[str, Any]) -> None:
    with contextlib.closing(sqlite3.connect(str(path), timeout=30)) as conn:
        conn.execute(
            """
            UPDATE async_runs
            SET state = ?, result_json = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (state, json.dumps(dict(result), sort_keys=True, separators=(",", ":")), time.time(), run_id),
        )
        conn.commit()


def _finish_if_running(path: Path, run_id: str, state: str, result: Mapping[str, Any]) -> bool:
    with contextlib.closing(sqlite3.connect(str(path), timeout=30)) as conn:
        cursor = conn.execute(
            """
            UPDATE async_runs
            SET state = ?, result_json = ?, updated_at = ?
            WHERE run_id = ? AND state = 'running'
            """,
            (state, json.dumps(dict(result), sort_keys=True, separators=(",", ":")), time.time(), run_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    engine_root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = engine_root if not existing else engine_root + os.pathsep + existing
    return env


def _log_worker_error(path: Path, run_id: str, exc: BaseException) -> None:
    try:
        log_path = path.parent / "async-worker.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{run_id} {type(exc).__name__}: {exc}\n")
    except OSError:
        pass


class _PayloadTimeout(Exception):
    pass


def main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[1] != "worker":
        return 2
    path = Path(argv[2])
    run_id = argv[3]
    spec = json.loads(argv[4])
    if not isinstance(spec, dict):
        return 2
    _worker_main(path, run_id, spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

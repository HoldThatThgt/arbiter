"""Async run worker substrate for the Arbiter engine."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Optional


class RunManager:
    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)
        self.db_path = self.repo / ".arbiter" / "runs" / "state.sqlite"

    def start_run(
        self, spec: Mapping[str, Any], meta: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        clean_spec = _validate_spec(spec)
        self._init_db()
        with closing(self._connect()) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO async_run(status,spec_json,result_json,meta_json)
                    VALUES('running', ?, NULL, ?)
                    """,
                    (
                        json.dumps(clean_spec, sort_keys=True, separators=(",", ":")),
                        json.dumps(dict(meta or {}), sort_keys=True, separators=(",", ":")),
                    ),
                )
                run_id = f"r-{cursor.lastrowid}"
                conn.execute(
                    "UPDATE async_run SET run_id = ? WHERE id = ?",
                    (run_id, cursor.lastrowid),
                )

        _launch_worker(self.db_path, run_id, clean_spec)
        return {"run_id": run_id, "status": "running"}

    def run_status(self, run_id: str) -> dict[str, Any]:
        self._init_db()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT status,result_json FROM async_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        status, result_json = row
        out: dict[str, Any] = {"run_id": run_id, "status": status}
        if result_json:
            out["result"] = json.loads(result_json)
        return out

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS async_run(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT UNIQUE,
                        status TEXT NOT NULL,
                        spec_json TEXT NOT NULL,
                        result_json TEXT,
                        meta_json TEXT NOT NULL
                    )
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=5)


def _validate_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise ValueError("run spec must be an object")
    allowed = {"duration_ms", "timeout_ms", "overall"}
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise ValueError(f"unknown run spec keys: {', '.join(unknown)}")

    duration_ms = spec.get("duration_ms", 0)
    timeout_ms = spec.get("timeout_ms", 600000)
    overall = spec.get("overall", "passed")
    if not _non_negative_int(duration_ms):
        raise ValueError("duration_ms must be a non-negative integer")
    if not _positive_int(timeout_ms):
        raise ValueError("timeout_ms must be a positive integer")
    if timeout_ms > 3600000:
        raise ValueError("timeout_ms must be at most 3600000")
    if not isinstance(overall, str):
        raise ValueError("overall must be a string")
    return {
        "duration_ms": duration_ms,
        "timeout_ms": timeout_ms,
        "overall": overall,
    }


def _launch_worker(db_path: Path, run_id: str, spec: Mapping[str, Any]) -> None:
    if not hasattr(os, "fork"):
        _run_worker_guarded(db_path, run_id, spec)
        return

    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return

    try:
        os.setsid()
        grandchild = os.fork()
        if grandchild:
            os._exit(0)
        os.execvpe(
            sys.executable,
            [
                sys.executable,
                "-m",
                "arbiter_engine.runs.worker",
                str(db_path),
                run_id,
                json.dumps(dict(spec), sort_keys=True, separators=(",", ":")),
            ],
            _worker_env(),
        )
    except BaseException as exc:
        _finish(
            db_path,
            run_id,
            "failed",
            {"run_id": run_id, "overall": "failed", "reason": type(exc).__name__},
        )
    finally:
        os._exit(0)


class _WorkerTimeout(Exception):
    pass


def _run_worker_guarded(db_path: Path, run_id: str, spec: Mapping[str, Any]) -> None:
    timer_armed = False
    try:
        timer_armed = _arm_timeout(int(spec["timeout_ms"]))
        _run_worker(db_path, run_id, spec)
    except _WorkerTimeout:
        _finish(
            db_path,
            run_id,
            "timeout",
            {"run_id": run_id, "overall": "failed", "reason": "timeout"},
        )
    except BaseException as exc:
        _finish(
            db_path,
            run_id,
            "failed",
            {"run_id": run_id, "overall": "failed", "reason": type(exc).__name__},
        )
    finally:
        if timer_armed:
            signal.setitimer(signal.ITIMER_REAL, 0)


def _arm_timeout(timeout_ms: int) -> bool:
    if not hasattr(signal, "setitimer"):
        return False

    def raise_timeout(signum: int, frame: Any) -> None:
        raise _WorkerTimeout()

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_ms / 1000)
    return True


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    engine_root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        engine_root if not existing else engine_root + os.pathsep + existing
    )
    return env


def _run_worker(db_path: Path, run_id: str, spec: Mapping[str, Any]) -> None:
    duration_ms = int(spec["duration_ms"])
    time.sleep(duration_ms / 1000)
    _finish(
        db_path,
        run_id,
        "finished",
        {"run_id": run_id, "overall": str(spec["overall"])},
    )


def _finish(db_path: Path, run_id: str, status: str, result: Mapping[str, Any]) -> None:
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        with conn:
            conn.execute(
                "UPDATE async_run SET status = ?, result_json = ? WHERE run_id = ?",
                (
                    status,
                    json.dumps(dict(result), sort_keys=True, separators=(",", ":")),
                    run_id,
                ),
            )


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0

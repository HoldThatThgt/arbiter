"""SQLite state for recipe-backed runs."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional


BUSY_TIMEOUT_MS = 30000

# DB paths whose schema has already been initialized by this process.
_INITIALIZED_DB_PATHS: set[str] = set()


@dataclass(frozen=True)
class TargetState:
    target_id: str
    spec_digest: str
    sources_digest: str
    doc_digest: str
    proven: bool
    proof_run_id: Optional[str]


@dataclass(frozen=True)
class ScannedTest:
    target_id: str
    suite: str
    name: str
    file: str
    line: int


def init(path: Path | str) -> None:
    db_path = Path(path)
    key = str(db_path.resolve())
    if key in _INITIALIZED_DB_PATHS:
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        _create_schema(conn)
        conn.commit()
    _INITIALIZED_DB_PATHS.add(key)


def connect(path: Path | str, *, timeout_s: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=timeout_s, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextlib.contextmanager
def transaction(
    path: Path | str,
    *,
    timeout_s: float = 30.0,
    trace: Optional[Callable[[str], None]] = None,
) -> Iterator[sqlite3.Connection]:
    init(path)
    conn = connect(path, timeout_s=timeout_s)
    if trace is not None:
        conn.set_trace_callback(trace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_run(
    path: Path | str,
    run_id: str,
    *,
    target_id: str,
    profile: str,
    overall: str,
    state: str = "completed",
    match_id: Optional[str] = None,
    task_id: Optional[str] = None,
    round: Optional[int] = None,
    payload: Optional[Mapping[str, Any]] = None,
) -> None:
    now = time.time()
    with transaction(path) as conn:
        conn.execute(
            """
            INSERT INTO run
                (run_id, match_id, task_id, round, target_id, profile, state, overall, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                match_id=excluded.match_id,
                task_id=excluded.task_id,
                round=excluded.round,
                target_id=excluded.target_id,
                profile=excluded.profile,
                state=excluded.state,
                overall=excluded.overall,
                finished_at=excluded.finished_at
            """,
            (run_id, match_id, task_id, round, target_id, profile, state, overall, now, now),
        )
        if payload is not None:
            conn.execute(
                """
                INSERT INTO run_payload (run_id, payload_json)
                VALUES (?, ?)
                ON CONFLICT(run_id) DO UPDATE SET payload_json=excluded.payload_json
                """,
                (run_id, json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))),
            )


def record_run_test(
    path: Path | str,
    run_id: str,
    *,
    suite: str,
    name: str,
    occurrence: int,
    status: str,
    elapsed_ms: int,
    file: Optional[str] = None,
    line: Optional[int] = None,
    message: Optional[str] = None,
) -> None:
    with transaction(path) as conn:
        conn.execute(
            """
            INSERT INTO run_test
                (run_id, suite, name, occurrence, status, elapsed_ms, file, line, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, suite, name, occurrence, status, elapsed_ms, file, line, message),
        )


def mark_target_proven(
    path: Path | str,
    *,
    target_id: str,
    spec_digest: str,
    sources_digest: str,
    proof_run_id: str,
    doc_digest: str = "",
) -> TargetState:
    with transaction(path) as conn:
        _write_target_state(
            conn,
            TargetState(
                target_id=target_id,
                spec_digest=spec_digest,
                sources_digest=sources_digest,
                doc_digest=doc_digest,
                proven=True,
                proof_run_id=proof_run_id,
            ),
        )
    return TargetState(target_id, spec_digest, sources_digest, doc_digest, True, proof_run_id)


def apply_target_revision(
    path: Path | str,
    *,
    target_id: str,
    spec_digest: str,
    sources_digest: str,
    doc_digest: str = "",
) -> TargetState:
    with transaction(path) as conn:
        row = conn.execute(
            """
            SELECT spec_digest, sources_digest, proven, proof_run_id
            FROM target_state
            WHERE target_id = ?
            """,
            (target_id,),
        ).fetchone()
        proven = False
        proof_run_id: Optional[str] = None
        if row is not None:
            old_spec, old_sources, old_proven, old_proof = row
            if bool(old_proven) and old_spec == spec_digest and old_sources == sources_digest:
                proven = True
                proof_run_id = old_proof
        result = TargetState(target_id, spec_digest, sources_digest, doc_digest, proven, proof_run_id)
        _write_target_state(conn, result)
        return result


def _write_target_state(conn: sqlite3.Connection, state: TargetState) -> None:
    conn.execute(
        """
        INSERT INTO target_state
            (target_id, spec_digest, sources_digest, doc_digest, proven, proof_run_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_id) DO UPDATE SET
            spec_digest=excluded.spec_digest,
            sources_digest=excluded.sources_digest,
            doc_digest=excluded.doc_digest,
            proven=excluded.proven,
            proof_run_id=excluded.proof_run_id,
            updated_at=excluded.updated_at
        """,
        (
            state.target_id,
            state.spec_digest,
            state.sources_digest,
            state.doc_digest,
            1 if state.proven else 0,
            state.proof_run_id,
            time.time(),
        ),
    )


def replace_scanned_tests(
    path: Path | str,
    target_id: str,
    candidates: Iterable[ScannedTest],
) -> tuple[ScannedTest, ...]:
    """Persist the scan result for ``target_id``, replacing any prior rows.

    A scan is a full snapshot of the candidates discovered for a scope, so the
    previous rows for the same ``target_id`` are cleared before the new set is
    inserted. Returns the persisted candidates in the deterministic
    (suite, name) order used by the round-trip reader.
    """
    rows = _dedupe_scanned(target_id, candidates)
    with transaction(path) as conn:
        conn.execute("DELETE FROM scanned_test WHERE target_id = ?", (target_id,))
        conn.executemany(
            """
            INSERT INTO scanned_test (target_id, suite, name, file, line)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(row.target_id, row.suite, row.name, row.file, row.line) for row in rows],
        )
    return rows


def read_scanned_tests(path: Path | str, target_id: str) -> tuple[ScannedTest, ...]:
    """Read back the persisted scan candidates for ``target_id``."""
    init(path)
    conn = connect(path)
    try:
        rows = conn.execute(
            """
            SELECT target_id, suite, name, file, line
            FROM scanned_test
            WHERE target_id = ?
            ORDER BY suite, name
            """,
            (target_id,),
        ).fetchall()
    finally:
        conn.close()
    return tuple(
        ScannedTest(target_id=row[0], suite=row[1], name=row[2], file=row[3], line=int(row[4]))
        for row in rows
    )


def _dedupe_scanned(
    target_id: str,
    candidates: Iterable[ScannedTest],
) -> tuple[ScannedTest, ...]:
    # scanned_test PK is (target_id, suite, name); a scope can surface the same
    # (suite, name) twice (e.g. parameterized instantiations), so collapse on the
    # key and keep deterministic order so the round-trip is stable.
    by_key: dict[tuple[str, str], ScannedTest] = {}
    for candidate in candidates:
        row = ScannedTest(
            target_id=target_id,
            suite=candidate.suite,
            name=candidate.name,
            file=candidate.file,
            line=candidate.line,
        )
        by_key[(row.suite, row.name)] = row
    return tuple(by_key[key] for key in sorted(by_key))


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS async_runs (
            run_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            result_json TEXT,
            worker_pid INTEGER,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scanned_test (
            target_id TEXT NOT NULL,
            suite TEXT NOT NULL,
            name TEXT NOT NULL,
            file TEXT NOT NULL,
            line INTEGER NOT NULL,
            PRIMARY KEY (target_id, suite, name)
        );

        CREATE TABLE IF NOT EXISTS run (
            run_id TEXT PRIMARY KEY,
            match_id TEXT,
            task_id TEXT,
            round INTEGER,
            target_id TEXT NOT NULL,
            profile TEXT NOT NULL,
            state TEXT NOT NULL,
            overall TEXT,
            started_at REAL NOT NULL,
            finished_at REAL
        );

        CREATE TABLE IF NOT EXISTS run_test (
            run_id TEXT NOT NULL,
            suite TEXT NOT NULL,
            name TEXT NOT NULL,
            occurrence INTEGER NOT NULL,
            status TEXT NOT NULL,
            elapsed_ms INTEGER NOT NULL,
            file TEXT,
            line INTEGER,
            message TEXT,
            PRIMARY KEY (run_id, suite, name, occurrence),
            FOREIGN KEY (run_id) REFERENCES run(run_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS run_payload (
            run_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES run(run_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS target_state (
            target_id TEXT PRIMARY KEY,
            spec_digest TEXT NOT NULL,
            sources_digest TEXT NOT NULL,
            doc_digest TEXT NOT NULL,
            proven INTEGER NOT NULL CHECK (proven IN (0, 1)),
            proof_run_id TEXT,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS compile_cache (
            key TEXT PRIMARY KEY,
            sources_digest TEXT NOT NULL,
            binary TEXT NOT NULL,
            built_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS run_correlation_idx
            ON run(match_id, task_id, round);
        """
    )

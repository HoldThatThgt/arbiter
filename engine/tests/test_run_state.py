import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from arbiter_engine.runs import state


class RunStateTest(unittest.TestCase):
    def test_schema_pragmas_and_core_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / ".arbiter" / "runs" / "state.sqlite"

            state.init(db)

            with sqlite3.connect(str(db)) as conn:
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(journal_mode, "wal")
            self.assertGreaterEqual(busy_timeout, 5000)
            self.assertTrue(
                {
                    "async_runs",
                    "scanned_test",
                    "run",
                    "run_test",
                    "run_payload",
                    "target_state",
                    "compile_cache",
                }.issubset(tables)
            )

    def test_run_test_occurrence_is_part_of_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            state.init(db)

            state.record_run(db, "r1", target_id="unit", profile="debug", overall="failed")
            state.record_run_test(
                db,
                "r1",
                suite="Suite",
                name="Repeated",
                occurrence=1,
                status="failed",
                elapsed_ms=3,
            )
            state.record_run_test(
                db,
                "r1",
                suite="Suite",
                name="Repeated",
                occurrence=2,
                status="passed",
                elapsed_ms=4,
            )

            with sqlite3.connect(str(db)) as conn:
                rows = conn.execute(
                    "SELECT occurrence, status FROM run_test ORDER BY occurrence"
                ).fetchall()
            self.assertEqual(rows, [(1, "failed"), (2, "passed")])
            with self.assertRaises(sqlite3.IntegrityError):
                state.record_run_test(
                    db,
                    "r1",
                    suite="Suite",
                    name="Repeated",
                    occurrence=2,
                    status="passed",
                    elapsed_ms=4,
                )

    def test_transaction_uses_begin_immediate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            state.init(db)
            statements = []

            with state.transaction(db, trace=statements.append) as conn:
                conn.execute("SELECT 1").fetchone()

            self.assertIn("BEGIN IMMEDIATE", statements[0])

    def test_transaction_rolls_back_on_exception_and_keeps_committed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            state.init(db)

            with state.transaction(db) as conn:
                conn.execute(
                    "INSERT INTO compile_cache (key, sources_digest, binary, built_at)"
                    " VALUES ('committed', 'd1', 'bin1', 1.0)"
                )

            with self.assertRaises(RuntimeError):
                with state.transaction(db) as conn:
                    conn.execute(
                        "INSERT INTO compile_cache (key, sources_digest, binary, built_at)"
                        " VALUES ('rolled-back', 'd2', 'bin2', 2.0)"
                    )
                    raise RuntimeError("abort mid-transaction")

            with sqlite3.connect(str(db)) as conn:
                keys = sorted(
                    row[0]
                    for row in conn.execute("SELECT key FROM compile_cache")
                )
            self.assertEqual(keys, ["committed"])

    def test_crashed_writer_mid_transaction_leaves_db_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            state.init(db)

            with state.transaction(db) as conn:
                conn.execute(
                    "INSERT INTO compile_cache (key, sources_digest, binary, built_at)"
                    " VALUES ('survivor', 'd1', 'bin1', 1.0)"
                )

            # A writer that dies mid-transaction (BEGIN IMMEDIATE + INSERT,
            # then hard exit without commit) must not corrupt the DB or leak
            # its uncommitted row.
            crasher = (
                "import os, sqlite3, sys\n"
                "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
                "conn.execute('BEGIN IMMEDIATE')\n"
                "conn.execute(\"INSERT INTO compile_cache\"\n"
                "             \" (key, sources_digest, binary, built_at)\"\n"
                "             \" VALUES ('torn', 'd2', 'bin2', 2.0)\")\n"
                "os._exit(1)\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", crasher, str(db)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertEqual(proc.returncode, 1)

            with sqlite3.connect(str(db)) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                keys = sorted(
                    row[0]
                    for row in conn.execute("SELECT key FROM compile_cache")
                )
            self.assertEqual(integrity, "ok")
            self.assertEqual(keys, ["survivor"])

    def test_concurrent_begin_immediate_writers_serialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            state.init(db)

            first_in_transaction = threading.Event()
            release_first = threading.Event()
            events = []
            errors = []

            def first_writer():
                try:
                    with state.transaction(db) as conn:
                        conn.execute(
                            "INSERT INTO compile_cache (key, sources_digest, binary, built_at)"
                            " VALUES ('first', 'd1', 'bin1', 1.0)"
                        )
                        first_in_transaction.set()
                        release_first.wait(timeout=10)
                    events.append("first_committed")
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            def second_writer():
                try:
                    first_in_transaction.wait(timeout=10)
                    with state.transaction(db) as conn:
                        # BEGIN IMMEDIATE only succeeds once the first writer
                        # has committed; reaching this line records the order.
                        events.append("second_in_transaction")
                        conn.execute(
                            "INSERT INTO compile_cache (key, sources_digest, binary, built_at)"
                            " VALUES ('second', 'd2', 'bin2', 2.0)"
                        )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            t1 = threading.Thread(target=first_writer)
            t2 = threading.Thread(target=second_writer)
            t1.start()
            t2.start()
            self.assertTrue(first_in_transaction.wait(timeout=10))
            # Give the second writer a moment to block on BEGIN IMMEDIATE
            # while the first transaction is still open.
            time.sleep(0.2)
            self.assertEqual(events, [])
            release_first.set()
            t1.join(timeout=15)
            t2.join(timeout=15)

            self.assertEqual(errors, [])
            self.assertEqual(sorted(events), ["first_committed", "second_in_transaction"])
            with sqlite3.connect(str(db)) as conn:
                keys = sorted(
                    row[0]
                    for row in conn.execute("SELECT key FROM compile_cache")
                )
            self.assertEqual(keys, ["first", "second"])

    def test_proven_lifecycle_preserves_doc_only_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            state.init(db)

            state.mark_target_proven(
                db,
                target_id="unit",
                spec_digest="spec-a",
                sources_digest="src-a",
                proof_run_id="r1",
                doc_digest="doc-a",
            )
            preserved = state.apply_target_revision(
                db,
                target_id="unit",
                spec_digest="spec-a",
                sources_digest="src-a",
                doc_digest="doc-b",
            )
            invalidated = state.apply_target_revision(
                db,
                target_id="unit",
                spec_digest="spec-b",
                sources_digest="src-a",
                doc_digest="doc-c",
            )

            self.assertTrue(preserved.proven)
            self.assertEqual(preserved.proof_run_id, "r1")
            self.assertEqual(preserved.doc_digest, "doc-b")
            self.assertFalse(invalidated.proven)
            self.assertIsNone(invalidated.proof_run_id)


if __name__ == "__main__":
    unittest.main()

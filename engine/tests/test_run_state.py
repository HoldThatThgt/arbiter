import sqlite3
import tempfile
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

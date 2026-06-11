import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from cipher2.cli import main
from cipher2.storage import FactRecord, SourceInventoryEntry, open_fact_store
from cipher2.tools.log import LogEvent, open_log


def _run(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(argv, stdout=stdout, stderr=stderr)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _fact(index: int = 1):
    return FactRecord(
        object_id=f"fact:{index}",
        object_name=f"Fact {index}",
        object_description="Status input",
        object_source=f"src/status_{index}.c:1",
        object_profile="debug",
        payload={"fact_kind": "function"},
    )


def _source(index: int = 1):
    return SourceInventoryEntry(
        source_id=f"source:{index}",
        rel_path=f"src/status_{index}.c",
        source_kind="c_source",
        sha256="a" * 64,
        size_bytes=12,
        mtime_ns=100 + index,
        compile_command_hash="b" * 64,
        toolchain_hash="c" * 64,
        included_by=[],
        includes=[],
    )


class CliStatusCommandTest(unittest.TestCase):
    def test_status_human_output_renders_sections_without_creating_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, stdout, stderr = _run(["status", str(target)])

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertIn(f"cipher-2 status: {target}", stdout)
            self.assertIn("state: empty", stdout)
            self.assertIn("storage: empty", stdout)
            self.assertIn("format: -  compression: -", stdout)
            self.assertIn("bytes: 0 compressed / 0 raw  ratio: 100.00%", stdout)
            self.assertIn("log: empty", stdout)
            self.assertIn("init stages: -", stdout)
            self.assertIn("incremental: disabled", stdout)
            self.assertNotIn("\x1b[", stdout)
            self.assertNotIn("Traceback", stdout)
            self.assertFalse((target / ".cipher" / "snapshots").exists())

    def test_status_json_output_is_full_tools_overview_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w")
            store.replace_snapshot([_fact(1), _fact(2)], [], [_source(1), _source(2)])
            open_log(target).write_event(
                LogEvent(
                    event_name="storage.search",
                    channel="storage",
                    counts={"matched_count": 2},
                    payload={"operation": "search"},
                )
            )
            open_log(target).write_event(
                LogEvent(
                    event_name="init.stage",
                    channel="initializer",
                    duration_ms=1.0,
                    counts={"source_count": 2},
                    payload={
                        "operation": "initialize_repository",
                        "outcome": "stage_completed",
                        "stage": "collect",
                        "stage_duration_ms": 1,
                    },
                )
            )

            exit_code, stdout, stderr = _run(["status", str(target), "--json"])
            payload = json.loads(stdout)

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertEqual(sorted(payload), ["errors", "generated_at", "incremental", "log", "state", "storage"])
            self.assertEqual(payload["storage"]["total_facts"], 2)
            self.assertEqual(payload["storage"]["total_sources"], 2)
            self.assertEqual(payload["storage"]["snapshot_format"], "compact-jsonl-gzip")
            self.assertEqual(payload["storage"]["compression"], "gzip-1")
            self.assertGreater(payload["storage"]["bytes_on_disk"], 0)
            self.assertGreater(payload["storage"]["uncompressed_bytes"], 0)
            self.assertIn("storage.search", dict(payload["log"]["top_event_names"]))
            self.assertEqual(payload["log"]["init_stage_timings"][0]["stage"], "collect")
            self.assertEqual(payload["errors"], [])

    def test_status_section_error_is_rendered_without_failing_or_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            snapshot_id = (target / ".cipher" / "snapshots" / "current").read_text(encoding="utf-8")
            (target / ".cipher" / "snapshots" / snapshot_id / "stats.json").write_text("{}", encoding="utf-8")

            exit_code, stdout, stderr = _run(["status", str(target)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("state: error", stdout)
            self.assertIn("storage: error", stdout)
            self.assertIn("error: storage_unreadable", stdout)
            self.assertIn("log:", stdout)
            self.assertNotIn("Traceback", stdout)

    def test_status_invalid_target_writes_stderr_without_status_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"

            exit_code, stdout, stderr = _run(["status", str(missing), "--json"])

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("cipher2: invalid_target:", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_status_log_write_failure_does_not_break_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "log").write_text("not a directory", encoding="utf-8")

            exit_code, stdout, stderr = _run(["status", str(target)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("cipher-2 status:", stdout)
            self.assertIn("log: error", stdout)
            self.assertNotIn("Traceback", stdout)


if __name__ == "__main__":
    unittest.main()

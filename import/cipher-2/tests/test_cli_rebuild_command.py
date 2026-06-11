import io
import json
import tempfile
import unittest
from pathlib import Path

from cipher2.cli import main
from cipher2.tools.log import open_log
from tests.toolchain_helpers import write_fake_toolchain


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(argv, stdout=stdout, stderr=stderr)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class CliRebuildCommandTest(unittest.TestCase):
    def test_rebuild_runs_full_snapshot_write_and_returns_rebuild_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write(target / "src" / "main.c", "int main(void) { return 0; }\n")
            write_fake_toolchain(target)
            first_exit, first_stdout, first_stderr = _run(["init", str(target), "--source-root", "src/main.c", "--json"])
            _write(
                target / "src" / "main.c",
                "int main(void) { return 0; }\nint added(void) { return 1; }\n",
            )

            exit_code, stdout, stderr = _run(["rebuild", str(target), "--source-root", "src/main.c", "--json"])
            first = json.loads(first_stdout)
            rebuilt = json.loads(stdout)

        self.assertEqual(first_exit, 0, first_stderr)
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(rebuilt["command"], "rebuild")
        self.assertTrue(rebuilt["ok"])
        self.assertGreater(rebuilt["fact_count"], first["fact_count"])
        self.assertNotEqual(rebuilt["snapshot_id"], first["snapshot_id"])

    def test_rebuild_writes_initializer_and_incremental_observability_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            exit_code, _stdout, stderr = _run(["rebuild", str(target), "--json"])
            initializer_events = open_log(target).read_events(channel="initializer").events
            incremental_events = open_log(target).read_events(channel="incremental").events

        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("initializer.rebuild", [event.event_name for event in initializer_events])
        self.assertIn("incremental.rebuild_published", [event.event_name for event in incremental_events])


if __name__ == "__main__":
    unittest.main()

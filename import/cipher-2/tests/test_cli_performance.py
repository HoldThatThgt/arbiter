import io
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from cipher2.cli import main
from tests.toolchain_helpers import write_fake_toolchain


WORKLOADS = [
    ("small", 100, 2, 20),
    ("medium", 1_000, 10, 40),
    ("large", 5_000, 25, 80),
]


def _write_fixture(target: Path, loc: int, files: int) -> None:
    source_dir = target / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    loc_per_file = max(1, loc // files)
    for index in range(files):
        lines = [
            f"#define LIMIT_{index} {index}\n",
            f"int global_{index} = {index};\n",
            f"int func_{index}(void) {{ return global_{index}; }}\n",
        ]
        for line in range(max(0, loc_per_file - len(lines))):
            lines.append(f"/* filler {index}:{line} */\n")
        (source_dir / f"unit_{index:05d}.c").write_text("".join(lines), encoding="utf-8")


class CliPerformanceTest(unittest.TestCase):
    def test_small_medium_large_cli_unit_workloads_stay_within_memory_budgets(self):
        for name, loc, files, memory_mb in WORKLOADS:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                _write_fixture(target, loc, files)
                write_fake_toolchain(target)
                stdout = io.StringIO()
                stderr = io.StringIO()

                tracemalloc.start()
                try:
                    exit_code = main(["init", str(target), "--no-log", "--json"], stdout=stdout, stderr=stderr)
                    status_stdout = io.StringIO()
                    status_stderr = io.StringIO()
                    status_exit = main(["status", str(target)], stdout=status_stdout, stderr=status_stderr)
                    json_stdout = io.StringIO()
                    json_stderr = io.StringIO()
                    json_exit = main(["status", str(target), "--json"], stdout=json_stdout, stderr=json_stderr)
                    _current, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

                self.assertEqual(exit_code, 0)
                self.assertEqual(status_exit, 0, status_stderr.getvalue())
                self.assertEqual(json_exit, 0, json_stderr.getvalue())
                self.assertIn("storage:", status_stdout.getvalue())
                self.assertIn('"storage":', json_stdout.getvalue())
                self.assertLess(peak / 1024 / 1024, memory_mb)


if __name__ == "__main__":
    unittest.main()

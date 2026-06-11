import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from cipher2.initializer import estimate_initializer_peak_bytes, initialize_repository
from tests.toolchain_helpers import write_fake_toolchain


def _write_fixture(root: Path, file_count: int, functions_per_file: int) -> None:
    source_dir = root / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    for index in range(file_count):
        lines = [f"#define LIMIT_{index} {index}\n", f"int global_{index} = {index};\n"]
        for function in range(functions_per_file):
            return_expr = f"global_{index}" if index == 0 and function == 0 else "func_0_0()"
            lines.append(f"int func_{index}_{function}(void) {{ return {return_expr}; }}\n")
        (source_dir / f"unit_{index:04d}.c").write_text("".join(lines), encoding="utf-8")


class InitializerPerformanceTest(unittest.TestCase):
    def test_memory_budget_formula_tracks_single_file_window_fact_buffer_and_margin(self):
        estimate = estimate_initializer_peak_bytes(
            max_file_bytes=1024,
            fact_count=100,
            relative_count=50,
            average_fact_bytes=256,
            streaming_write=False,
            safety_margin_bytes=4096,
        )
        self.assertEqual(estimate, 1024 + 100 * 256 + 50 * 256 + 4096)

        streaming_estimate = estimate_initializer_peak_bytes(
            max_file_bytes=1024,
            fact_count=100,
            relative_count=50,
            function_fact_count=20,
            staging_window_count=10,
            average_fact_bytes=256,
            streaming_write=True,
            safety_margin_bytes=4096,
        )
        self.assertEqual(streaming_estimate, 1024 + (20 + 10) * 256 + 10 * 256 + 4096)

    def test_small_medium_large_unit_workloads_stay_within_memory_budgets(self):
        workloads = [
            ("small", 1, 1, 64, 5.0),
            ("medium", 10, 3, 64, 10.0),
            ("large", 50, 5, 64, 20.0),
        ]
        for name, file_count, functions_per_file, memory_mb, timeout_seconds in workloads:
            with self.subTest(workload=name):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)
                    _write_fixture(target, file_count, functions_per_file)
                    write_fake_toolchain(target)

                    tracemalloc.start()
                    started = time.perf_counter()
                    try:
                        summary = initialize_repository(target, log_enabled=False)
                        _current, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                    elapsed = time.perf_counter() - started

                    self.assertTrue(summary.ok)
                    self.assertEqual(summary.source_count, file_count)
                    self.assertLess(peak / 1024 / 1024, memory_mb)
                    self.assertLess(elapsed, timeout_seconds)


if __name__ == "__main__":
    unittest.main()

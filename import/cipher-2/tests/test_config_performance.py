import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from cipher2.config import load_config, write_default_config


class ConfigPerformanceTest(unittest.TestCase):
    def test_small_medium_large_config_loads_stay_within_memory_budgets(self):
        workloads = [
            ("small", 100, 5, 1.0),
            ("medium", 1_000, 40, 5.0),
            ("large", 5_000, 80, 15.0),
        ]
        for name, iterations, memory_mb, timeout_seconds in workloads:
            with self.subTest(workload=name):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)
                    compile_db = target / "build" / "compile_commands.json"
                    compile_db.parent.mkdir()
                    compile_db.write_text("[]", encoding="utf-8")
                    write_default_config(target, compile_database="build/compile_commands.json", observe=False)
                    expected_compile_db = compile_db.resolve(strict=False)

                    tracemalloc.start()
                    started = time.perf_counter()
                    try:
                        for _index in range(iterations):
                            config = load_config(target, observe=False)
                            self.assertEqual(config.compile_database_path, expected_compile_db)
                        _current, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                    elapsed = time.perf_counter() - started

                    self.assertLess(peak / 1024 / 1024, memory_mb)
                    self.assertLess(elapsed, timeout_seconds)


if __name__ == "__main__":
    unittest.main()

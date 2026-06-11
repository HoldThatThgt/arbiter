import tempfile
import tracemalloc
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cipher2.tools.log import LogEvent, open_log


class LogPerformanceTest(unittest.TestCase):
    def test_small_medium_large_workloads_stay_within_memory_budgets(self):
        workloads = [
            ("small", 100, 5),
            ("medium", 2000, 40),
            ("large", 8000, 80),
        ]

        for name, event_count, memory_limit_mb in workloads:
            with self.subTest(workload=name), tempfile.TemporaryDirectory() as tmp:
                log = open_log(Path(tmp))
                tracemalloc.start()
                try:
                    for index in range(event_count):
                        log.write_event(
                            LogEvent(
                                event_name="storage.search",
                                channel="storage",
                                duration_ms=float(index % 17),
                                counts={"matched_count": index % 5},
                                payload={
                                    "query_kind": "substring",
                                    "api_key": "secret",
                                    "items": list(range(index % 60)),
                                },
                            )
                        )
                    summary = log.summarize(channel="storage")
                    limited = log.read_events(channel="storage", limit=5)
                    _current, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

                self.assertEqual(summary.total_events, event_count)
                self.assertEqual(len(summary.recent_events), min(20, event_count))
                self.assertLessEqual(len(summary.slow_events), 20)
                self.assertEqual(len(limited.events), 5)
                self.assertLess(peak, memory_limit_mb * 1024 * 1024)

    def test_concurrent_appends_preserve_complete_jsonl_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            log = open_log(target)

            def write_one(index: int):
                log.write_event(
                    LogEvent(
                        event_name="storage.write",
                        channel="storage",
                        summary=f"event-{index}",
                        counts={"write_count": 1},
                    )
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write_one, range(400)))

            path = target / ".cipher" / "log" / "storage.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            summary = log.summarize(channel="storage")

            self.assertEqual(len(lines), 400)
            self.assertEqual(summary.total_events, 400)
            self.assertEqual(summary.custom_counts["write_count"], 400)


if __name__ == "__main__":
    unittest.main()

import tempfile
import tracemalloc
import unittest
from pathlib import Path

from cipher2.storage import FactRecord, open_fact_store
from cipher2.tools.log import LogEvent, open_log
from cipher2.tools.views import build_overview


def _facts(count: int):
    for index in range(count):
        yield FactRecord(
            object_id=f"fact:{index:06d}",
            object_name=f"Fact {index}",
            object_description=f"Views performance fact {index}",
            object_source=f"src/module{index % 5}.py:{index}",
            object_profile="debug" if index % 2 else "release",
            payload={"fact_kind": "function" if index % 2 else "doc"},
        )


class ViewsPerformanceTest(unittest.TestCase):
    def test_small_medium_large_view_builds_stay_within_memory_budgets(self):
        workloads = [
            ("small", 100, 100, 5),
            ("medium", 2000, 2000, 40),
            ("large", 8000, 8000, 80),
        ]

        for name, fact_count, event_count, memory_limit_mb in workloads:
            with self.subTest(workload=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                open_fact_store(target, mode="w", log_enabled=False).replace_facts(_facts(fact_count))
                log = open_log(target)
                for index in range(event_count):
                    log.write_event(
                        LogEvent(
                            event_name="mcp.search",
                            channel="mcp",
                            duration_ms=float(index % 19),
                            counts={"matched_count": index % 7},
                            payload={"query_kind": "substring", "limit": 20},
                        )
                    )

                tracemalloc.start()
                try:
                    overview = build_overview(target, top_n=10)
                    _current, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

                self.assertEqual(overview.state, "ready")
                self.assertEqual(overview.storage.total_facts, fact_count)
                self.assertEqual(overview.log.total_events, event_count)
                self.assertLessEqual(len(overview.log.recent_events), 20)
                self.assertLessEqual(len(overview.log.slow_events), 20)
                self.assertLess(peak, memory_limit_mb * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()

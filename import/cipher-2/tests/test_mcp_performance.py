import tempfile
import tracemalloc
import unittest
from pathlib import Path

from cipher2.mcp import open_mcp_server
from cipher2.storage import FactRecord, open_fact_store


def _facts(count: int):
    for index in range(count):
        yield FactRecord(
            object_id=f"fact:{index:06d}",
            object_name=f"Function {index}",
            object_description=f"Searchable alpha beta {index}",
            object_source=f"src/module{index % 7}.c:{index + 1}",
            object_profile="debug" if index % 2 else "release",
            object_caller=f"caller:{index % 11}" if index % 3 == 0 else None,
            object_callee=f"callee:{index % 13}" if index % 5 == 0 else None,
            payload={"fact_kind": "function", "rank": index, "body": "x" * 64},
        )


class McpPerformanceTest(unittest.TestCase):
    def test_small_medium_large_unit_workloads_stay_within_memory_budgets(self):
        workloads = [
            ("small", 100, 5, 8),
            ("medium", 2000, 40, 16),
            ("large", 8000, 80, 32),
        ]

        for name, fact_count, memory_limit_mb, calls in workloads:
            with self.subTest(workload=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                store = open_fact_store(target, mode="w", log_enabled=False)
                store.replace_facts(_facts(fact_count))
                server = open_mcp_server(target)

                tracemalloc.start()
                try:
                    for index in range(calls):
                        search = server.search("alpha", limit=5)
                        self.assertEqual(search.result_count, 5)
                        detail = server.detail(search.results[index % len(search.results)].object_id, budget="small")
                        self.assertEqual(detail.fact.object_id, search.results[index % len(search.results)].object_id)
                    _current, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

                self.assertLess(peak, memory_limit_mb * 1024 * 1024)

    def test_low_limit_search_keeps_response_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts(_facts(1000))

            tracemalloc.start()
            try:
                result = open_mcp_server(target).search("alpha", limit=1)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertEqual(result.result_count, 1)
        self.assertLess(peak, 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()

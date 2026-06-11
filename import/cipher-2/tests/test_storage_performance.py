import tempfile
import tracemalloc
import unittest
from pathlib import Path

from cipher2.storage import FactRecord, open_fact_store


def _facts(count: int):
    for index in range(count):
        yield FactRecord(
            object_id=f"fact:{index:06d}",
            object_name=f"Fact {index}",
            object_description=f"Searchable alpha beta {index}",
            object_source=f"src/module{index % 7}.py:{index}",
            object_profile="debug" if index % 2 else "release",
            object_caller=f"caller:{index % 11}" if index % 3 == 0 else None,
            object_callee=f"callee:{index % 13}" if index % 5 == 0 else None,
            payload={"fact_kind": "function" if index % 2 else "doc", "rank": index},
        )


class StoragePerformanceTest(unittest.TestCase):
    def test_small_medium_large_workloads_stay_within_memory_budgets(self):
        workloads = [
            ("small", 100, 5),
            ("medium", 2000, 40),
            ("large", 8000, 80),
        ]

        for name, fact_count, memory_limit_mb in workloads:
            with self.subTest(workload=name), tempfile.TemporaryDirectory() as tmp:
                store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
                tracemalloc.start()
                try:
                    manifest = store.replace_facts(_facts(fact_count))
                    results = store.search("alpha", limit=5)
                    stats = store.stats()
                    _current, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

                self.assertEqual(manifest.fact_count, fact_count)
                self.assertEqual(stats.total_facts, fact_count)
                self.assertEqual(len(results), 5)
                self.assertLess(peak, memory_limit_mb * 1024 * 1024)
                if fact_count >= 1000:
                    self.assertLessEqual(stats.bytes_on_disk, stats.uncompressed_bytes * 0.6)
                    self.assertLessEqual(stats.read_index_bytes, stats.compressed_data_bytes * 2)

    def test_search_low_limit_does_not_materialize_extra_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            store.replace_facts(_facts(1000))

            tracemalloc.start()
            try:
                results = store.search("alpha", limit=1)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertEqual(len(results), 1)
            self.assertLess(peak, 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()

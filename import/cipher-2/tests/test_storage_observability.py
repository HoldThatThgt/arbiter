import tempfile
import unittest
from pathlib import Path

from cipher2.storage import FactRecord, StorageError, open_fact_store
from cipher2.tools.log import open_log


def _fact():
    return FactRecord(
        object_id="fact:one",
        object_name="One",
        object_description="One fact",
        object_source="src/one.py:1",
        object_profile="debug",
        payload={"fact_kind": "function", "secret_token": "must-not-log"},
    )


class StorageObservabilityTest(unittest.TestCase):
    def test_replace_facts_writes_storage_write_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            manifest = open_fact_store(target, mode="w").replace_facts([_fact()])

            events = open_log(target).read_events(channel="storage").events
            write = next(event for event in events if event.event_name == "storage.write")
            self.assertEqual(write.status, "ok")
            self.assertEqual(write.payload["operation"], "replace_facts")
            self.assertEqual(write.payload["outcome"], "created")
            self.assertEqual(write.payload["snapshot_id"], manifest.snapshot_id)
            self.assertEqual(write.payload["fact_count"], 1)
            self.assertEqual(write.payload["snapshot_format"], "compact-jsonl-gzip")
            self.assertEqual(write.payload["compression"], "gzip-1")
            self.assertEqual(write.payload["read_index_format"], "sqlite-read-index")
            self.assertEqual(write.payload["read_index_codec"], "json-text")
            self.assertEqual(write.counts["bytes_written"], manifest.bytes_on_disk)
            self.assertEqual(write.counts["uncompressed_bytes"], manifest.uncompressed_bytes)
            self.assertEqual(write.counts["compressed_data_bytes"], manifest.compressed_data_bytes)
            self.assertEqual(write.counts["read_index_bytes"], manifest.read_index["bytes_on_disk"])
            self.assertIn("read_index_build_ms", write.counts)
            self.assertEqual(
                write.counts["storage_overhead_ratio_percent"],
                round(manifest.bytes_on_disk * 100 / manifest.uncompressed_bytes),
            )
            self.assertEqual(
                write.counts["compression_ratio_percent"],
                round(manifest.compressed_data_bytes * 100 / manifest.uncompressed_bytes),
            )
            self.assertEqual(write.counts["facts_raw_bytes"], manifest.file_bytes["facts"]["raw_bytes"])
            self.assertEqual(write.counts["facts_compressed_bytes"], manifest.file_bytes["facts"]["compressed_bytes"])
            self.assertEqual(write.counts["relatives_raw_bytes"], manifest.file_bytes["relatives"]["raw_bytes"])
            self.assertEqual(write.counts["relatives_compressed_bytes"], manifest.file_bytes["relatives"]["compressed_bytes"])
            self.assertEqual(
                write.counts["source_inventory_raw_bytes"],
                manifest.file_bytes["source_inventory"]["raw_bytes"],
            )
            self.assertEqual(
                write.counts["source_inventory_compressed_bytes"],
                manifest.file_bytes["source_inventory"]["compressed_bytes"],
            )
            self.assertNotIn("must-not-log", str(write.to_json()))

    def test_first_search_writes_storage_index_open_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w")
            manifest = store.replace_facts([_fact()])

            store.search("one", limit=1)
            events = open_log(target).read_events(channel="storage").events
            index_open = [event for event in events if event.event_name == "storage.index_open"]

            self.assertEqual(len(index_open), 1)
            self.assertEqual(index_open[0].payload["index_backend"], "persistent-sqlite")
            self.assertEqual(index_open[0].payload["outcome"], "opened")
            self.assertEqual(index_open[0].counts["read_index_bytes"], manifest.read_index["bytes_on_disk"])
            self.assertIn("read_index_open_ms", index_open[0].counts)

    def test_idempotent_replace_writes_skipped_idempotent_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w")
            store.replace_facts([_fact()])
            store.replace_facts([_fact()])

            outcomes = [
                event.payload["outcome"]
                for event in open_log(target).read_events(channel="storage").events
                if event.event_name == "storage.write"
            ]
            self.assertEqual(outcomes, ["created", "skipped_idempotent"])

    def test_search_writes_query_observability_without_raw_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w")
            store.replace_facts([_fact()])

            store.search("x" * 200, limit=20)

            search = [
                event for event in open_log(target).read_events(channel="storage").events
                if event.event_name == "storage.search"
            ][0]
            self.assertEqual(search.payload["operation"], "search")
            self.assertEqual(search.payload["outcome"], "searched")
            self.assertEqual(search.payload["query_kind"], "terms")
            self.assertEqual(search.payload["term_count"], 1)
            self.assertEqual(len(search.payload["query_preview"]), 80)
            self.assertNotIn("query_sha256", search.payload)
            self.assertEqual(search.payload["limit"], 20)
            self.assertEqual(search.payload["matched_count"], 0)

    def test_search_failure_writes_storage_error_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w")
            store.replace_facts([_fact()])

            with self.assertRaises(StorageError):
                store.search("", limit=0)

            error = [
                event for event in open_log(target).read_events(channel="storage").events
                if event.event_name == "storage.error"
            ][0]
            self.assertEqual(error.status, "error")
            self.assertEqual(error.error_code, "invalid_limit")
            self.assertEqual(error.payload["outcome"], "failed")

    def test_log_write_failure_does_not_break_snapshot_and_is_exposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "log").write_text("not a directory", encoding="utf-8")

            manifest = open_fact_store(target, mode="w").replace_facts([_fact()])
            stats = open_fact_store(target, mode="r").stats()

            self.assertEqual(manifest.fact_count, 1)
            self.assertEqual(manifest.log_write_failures, 1)
            self.assertEqual(manifest.latest_log_error_code, "log_write_failed")
            self.assertEqual(stats.log_write_failures, 1)
            self.assertEqual(stats.latest_log_error_code, "log_write_failed")


if __name__ == "__main__":
    unittest.main()

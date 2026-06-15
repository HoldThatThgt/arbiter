# Migrated from cipher-2 tests/test_storage_file_store.py (M4 acceptance — imports rewritten cipher2.*->arbiter_engine.facts.*, .cipher->.arbiter/facts).
import gzip
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import arbiter_engine.facts.store as storage_module
from arbiter_engine.facts.store import FactRecord, FactRelative, StorageError, open_fact_store


def _fact(index: int, **overrides):
    data = {
        "object_id": f"fact:{index:03d}",
        "object_name": f"Fact {index}",
        "object_description": f"Alpha helper {index}",
        "object_source": f"src/module{index % 2}.py:{index}",
        "object_profile": "debug" if index % 2 else "release",
        "object_caller": "entry" if index % 2 else None,
        "object_callee": "worker" if index % 3 == 0 else None,
        "payload": {"fact_kind": "function" if index % 2 else "doc", "rank": index},
    }
    data.update(overrides)
    return FactRecord(**data)


def _relative(index: int, **overrides):
    data = {
        "relative_id": f"rel:{index:03d}",
        "from_fact_id": "fact:001",
        "to_fact_id": "fact:002",
        "relation_kind": "direct_call",
        "condition": None,
        "object_profile": "debug",
        "evidence_source": f"src/module.py:{index}",
        "confidence": 1.0,
        "payload": {"rank": index},
    }
    data.update(overrides)
    return FactRelative(**data)


class StorageFileStoreTest(unittest.TestCase):
    def test_empty_store_iter_get_search_and_stats_are_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="r", log_enabled=False)

            self.assertEqual(list(store.iter_facts()), [])
            self.assertIsNone(store.get_fact("missing"))
            self.assertEqual(store.search("", limit=10), [])
            stats = store.stats()
            self.assertEqual(stats.total_facts, 0)
            self.assertEqual(stats.snapshot_id, None)
            self.assertEqual(stats.snapshot_format, None)
            self.assertEqual(stats.compression, None)
            self.assertEqual(stats.uncompressed_bytes, 0)
            self.assertEqual(stats.compression_ratio, 1.0)
            self.assertEqual(stats.file_bytes, {})
            self.assertEqual(stats.lock_state, "free")
            self.assertFalse((Path(tmp) / ".arbiter" / "facts" / "snapshots").exists())

    def test_replace_facts_writes_v5_gzip_snapshot_current_manifest_stats_index_and_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)

            manifest = store.replace_facts([_fact(2), _fact(1)])

            current = target / ".arbiter" / "facts" / "snapshots" / "current"
            snapshot_dir = target / ".arbiter" / "facts" / "snapshots" / manifest.snapshot_id
            self.assertTrue(current.exists())
            self.assertEqual(current.read_text(encoding="utf-8"), manifest.snapshot_id)
            self.assertTrue((snapshot_dir / "facts.jsonl.gz").exists())
            self.assertTrue((snapshot_dir / "relatives.jsonl.gz").exists())
            self.assertTrue((snapshot_dir / "source_inventory.jsonl.gz").exists())
            self.assertTrue((snapshot_dir / "read_index.sqlite").exists())
            self.assertFalse((snapshot_dir / "facts.jsonl").exists())
            self.assertFalse((snapshot_dir / "relatives.jsonl").exists())
            self.assertFalse((snapshot_dir / "source_inventory.jsonl").exists())
            self.assertFalse((snapshot_dir / "read_index.sqlite-wal").exists())
            self.assertFalse((snapshot_dir / "read_index.sqlite-shm").exists())
            self.assertFalse((snapshot_dir / "read_index.sqlite-journal").exists())
            self.assertTrue((snapshot_dir / "manifest.json").exists())
            self.assertTrue((snapshot_dir / "stats.json").exists())
            self.assertFalse((snapshot_dir / "graph_objects.jsonl").exists())
            self.assertFalse((snapshot_dir / "graph_relatives.jsonl").exists())
            self.assertFalse((snapshot_dir / "graph_derived_from.jsonl").exists())
            self.assertEqual(manifest.schema_version, 5)
            self.assertEqual(manifest.snapshot_format, "compact-jsonl-gzip")
            self.assertEqual(manifest.compression, "gzip-1")
            self.assertEqual(manifest.fact_count, 2)
            self.assertEqual(manifest.relative_count, 0)
            self.assertGreater(manifest.uncompressed_bytes, 0)
            self.assertGreater(manifest.compressed_data_bytes, 0)
            self.assertGreater(manifest.read_index["bytes_on_disk"], 0)
            self.assertEqual(manifest.read_index["file_name"], "read_index.sqlite")
            self.assertEqual(manifest.read_index["index_format"], "sqlite-read-index")
            self.assertEqual(manifest.read_index["projection_kind"], "proxy-key-column-projection")
            self.assertEqual(manifest.read_index["schema_version"], 6)
            self.assertEqual(manifest.read_index["payload_codec"], "json-text")
            self.assertEqual(manifest.read_index["fact_count"], 2)
            self.assertEqual(manifest.read_index["relative_count"], 0)
            self.assertEqual(set(manifest.file_bytes), {"facts", "relatives", "source_inventory"})
            self.assertEqual(manifest.file_bytes["facts"]["file_name"], "facts.jsonl.gz")
            self.assertGreater(manifest.file_bytes["facts"]["raw_bytes"], 0)
            self.assertGreater(manifest.file_bytes["facts"]["compressed_bytes"], 0)
            self.assertFalse(manifest.reused)
            self.assertEqual(manifest.stats["total_facts"], 2)
            self.assertEqual(manifest.stats["total_relatives"], 0)
            self.assertEqual(manifest.stats["snapshot_format"], "compact-jsonl-gzip")
            self.assertEqual(manifest.stats["compression"], "gzip-1")
            self.assertEqual(manifest.stats["uncompressed_bytes"], manifest.uncompressed_bytes)
            self.assertEqual(manifest.stats["compressed_data_bytes"], manifest.compressed_data_bytes)
            self.assertEqual(manifest.stats["read_index_state"], "ready")
            self.assertEqual(manifest.stats["read_index_bytes"], manifest.read_index["bytes_on_disk"])
            self.assertEqual(manifest.stats["read_index_schema_version"], 6)
            self.assertEqual(manifest.stats["read_index_codec"], "json-text")
            self.assertEqual(manifest.stats["storage_overhead_ratio"], manifest.storage_overhead_ratio)
            self.assertEqual(manifest.stats["file_bytes"], manifest.file_bytes)
            self.assertAlmostEqual(
                manifest.stats["compression_ratio"],
                manifest.compressed_data_bytes / manifest.uncompressed_bytes,
                places=2,
            )
            self.assertAlmostEqual(
                manifest.stats["storage_overhead_ratio"],
                manifest.bytes_on_disk / manifest.uncompressed_bytes,
                places=2,
            )
            manifest_json = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
            stats_json = json.loads((snapshot_dir / "stats.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest_json["stats"], manifest.stats)
            self.assertEqual(manifest_json["stats"], stats_json)
            with gzip.open(snapshot_dir / "facts.jsonl.gz", "rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual([row["object_id"] for row in rows], ["fact:001", "fact:002"])
            self.assertEqual(rows[0]["schema_version"], 5)
            with sqlite3.connect(snapshot_dir / "read_index.sqlite") as connection:
                fact_columns = [row[1] for row in connection.execute("PRAGMA table_info(facts)")]
                self.assertEqual(fact_columns[0], "object_id")
                self.assertEqual(
                    [row[1] for row in connection.execute("PRAGMA table_info(fact_keys)")],
                    ["fact_k", "object_id"],
                )
                self.assertEqual(
                    [row[0] for row in connection.execute("SELECT object_id FROM facts ORDER BY object_id")],
                    ["fact:001", "fact:002"],
                )
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM fact_keys").fetchone()[0], 0)

    def test_iter_get_search_and_stats_use_current_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            store.replace_facts(
                [
                    _fact(1, object_id="fact:a", object_name="Alpha", object_description="entry point"),
                    _fact(2, object_id="fact:b", object_name="Beta", object_description="Alpha caller", object_caller="Alpha"),
                    _fact(3, object_id="fact:c", object_name="Gamma", object_description="unrelated", object_caller=None),
                ]
            )

            facts = list(store.iter_facts())
            self.assertEqual([fact.object_id for fact in facts], ["fact:a", "fact:b", "fact:c"])
            self.assertEqual(store.get_fact("fact:b").object_name, "Beta")
            self.assertIsNone(store.get_fact("missing"))
            self.assertEqual([fact.object_id for fact in store.search("alpha", limit=2)], ["fact:a", "fact:b"])
            stats = store.stats()
            self.assertEqual(stats.total_facts, 3)
            self.assertEqual(stats.total_relatives, 0)
            self.assertEqual(stats.snapshot_format, "compact-jsonl-gzip")
            self.assertEqual(stats.compression, "gzip-1")
            self.assertGreater(stats.uncompressed_bytes, 0)
            self.assertGreater(stats.bytes_on_disk, 0)
            self.assertGreater(stats.compression_ratio, 0)
            self.assertEqual(stats.read_index_state, "ready")
            self.assertGreater(stats.read_index_bytes, 0)
            self.assertEqual(stats.read_index_schema_version, 6)
            self.assertEqual(stats.read_index_codec, "json-text")
            self.assertGreater(stats.storage_overhead_ratio, 0)
            self.assertEqual(stats.relation_kinds, {})
            self.assertEqual(stats.fact_kinds, {"doc": 1, "function": 2})
            self.assertEqual(stats.profiles, {"debug": 2, "release": 1})
            self.assertEqual(stats.with_caller_count, 2)

    def test_same_content_reuses_snapshot_and_keeps_current_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)

            first = store.replace_facts([_fact(1), _fact(2)])
            second = store.replace_facts([_fact(1), _fact(2)])

            self.assertEqual(second.snapshot_id, first.snapshot_id)
            self.assertTrue(second.reused)
            self.assertEqual(second.created_at, first.created_at)
            self.assertEqual(store.stats().snapshot_count, 1)

    def test_replace_rejects_duplicate_object_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)

            with self.assertRaises(StorageError) as caught:
                store.replace_facts([_fact(1), _fact(2, object_id="fact:001")])

            self.assertEqual(caught.exception.code, "duplicate_object_id")

    def test_replace_snapshot_sorted_unique_bypasses_re_sort_and_validates_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            store = open_fact_store(target, mode="w", log_enabled=False)

            with mock.patch.object(
                storage_module.FileFactStore,
                "_prepare_snapshot_staging",
                side_effect=AssertionError("sorted-unique path must not re-sort through storage staging"),
            ):
                manifest = store.replace_snapshot_sorted_unique([_fact(1), _fact(2)], [], [])

            self.assertEqual(manifest.fact_count, 2)
            self.assertEqual([fact.object_id for fact in store.iter_facts()], ["fact:001", "fact:002"])

            with self.assertRaises(StorageError) as duplicate:
                store.replace_snapshot_sorted_unique([_fact(1), _fact(1)], [], [])
            self.assertEqual(duplicate.exception.code, "duplicate_object_id")

            with self.assertRaises(StorageError) as unsorted:
                store.replace_snapshot_sorted_unique([_fact(2), _fact(1)], [], [])
            self.assertEqual(unsorted.exception.code, "unsorted_object_id")

    def test_preencoded_sorted_unique_path_writes_same_snapshot_without_reencoding_records(self):
        facts = [_fact(1), _fact(2)]
        relatives = [_relative(1)]

        with tempfile.TemporaryDirectory() as baseline_tmp, tempfile.TemporaryDirectory() as encoded_tmp:
            baseline_store = open_fact_store(Path(baseline_tmp), mode="w", log_enabled=False)
            baseline = baseline_store.replace_snapshot_sorted_unique(facts, relatives, [])
            encoded_facts = [storage_module.EncodedFactLine.from_fact(fact) for fact in facts]
            encoded_relatives = [storage_module.EncodedRelativeLine.from_relative(relative) for relative in relatives]

            encoded_store = open_fact_store(Path(encoded_tmp), mode="w", log_enabled=False)
            with mock.patch.object(
                storage_module.StoredFactLine,
                "from_fact",
                side_effect=AssertionError("preencoded path must not rebuild fact snapshot lines"),
            ), mock.patch.object(
                storage_module.StoredRelativeLine,
                "from_relative",
                side_effect=AssertionError("preencoded path must not rebuild relative snapshot lines"),
            ):
                encoded = encoded_store._replace_snapshot_preencoded_sorted_unique(
                    encoded_facts,
                    encoded_relatives,
                    [],
                )

            self.assertEqual(encoded.snapshot_id, baseline.snapshot_id)
            self.assertEqual(encoded.facts_sha256, baseline.facts_sha256)
            self.assertEqual(encoded.relatives_sha256, baseline.relatives_sha256)
            self.assertEqual([fact.object_id for fact in encoded_store.iter_facts()], ["fact:001", "fact:002"])
            self.assertEqual([relative.relative_id for relative in encoded_store.iter_relatives()], ["rel:001"])

    def test_read_only_replace_and_invalid_mode_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(StorageError) as invalid:
                open_fact_store(Path(tmp), mode="x")
            self.assertEqual(invalid.exception.code, "invalid_mode")

            store = open_fact_store(Path(tmp), mode="r", log_enabled=False)
            with self.assertRaises(StorageError) as read_only:
                store.replace_facts([_fact(1)])
            self.assertEqual(read_only.exception.code, "read_only")

    def test_search_rejects_invalid_query_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            store.replace_facts([_fact(1)])

            with self.assertRaises(StorageError) as bad_query:
                store.search(12, limit=1)
            self.assertEqual(bad_query.exception.code, "invalid_query")

            with self.assertRaises(StorageError) as bad_limit:
                store.search("", limit=0)
            self.assertEqual(bad_limit.exception.code, "invalid_limit")

    def test_search_splits_terms_and_requires_all_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            store.replace_facts(
                [
                    _fact(1, object_id="fact:free", object_name="free_buffer", object_description="release memory"),
                    _fact(2, object_id="fact:member", object_name="touch_member", object_description="field access"),
                    _fact(3, object_id="fact:both", object_name="free_member", object_description="release member field"),
                    _fact(4, object_id="fact:split", object_name="free", object_description="uses member in source"),
                ]
            )

            first = [fact.object_id for fact in store.search("free member", limit=10)]
            second = [fact.object_id for fact in store.search("member free", limit=10)]

        self.assertEqual(first, ["fact:both", "fact:split"])
        self.assertEqual(second, first)

    def test_search_preserves_unicode_casefold_semantics_without_dense_casefold_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            store.replace_facts(
                [
                    _fact(1, object_id="fact:unicode", object_name="StraßeReader"),
                    _fact(2, object_id="fact:plain", object_name="PlainReader"),
                ]
            )

            self.assertEqual([fact.object_id for fact in store.search("strasse", limit=5)], ["fact:unicode"])

    def test_search_promotes_exact_type_and_function_over_same_named_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            facts = [
                _fact(1, object_id="type:client", object_name="client", payload={"fact_kind": "type"}),
                _fact(2, object_id="function:client", object_name="client", payload={"fact_kind": "function"}),
            ]
            facts.extend(
                _fact(
                    index + 10,
                    object_id=f"field:client:{index:02d}",
                    object_name="client",
                    object_description="client field reference",
                    payload={"fact_kind": "field", "owner_name": f"Owner{index}"},
                )
                for index in range(30)
            )
            store.replace_facts(facts)

            results = store.search("client", limit=20)

            self.assertEqual([fact.object_id for fact in results[:2]], ["type:client", "function:client"])
            self.assertIn("type:client", [fact.object_id for fact in results])
            self.assertIn("function:client", [fact.object_id for fact in results])
            self.assertGreaterEqual(
                sum(1 for fact in results if fact.object_id.startswith("field:client:")),
                3,
            )

    def test_search_keeps_exact_field_reachable_when_high_rank_kinds_share_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            facts = [
                _fact(
                    index,
                    object_id=f"function:next:{index:02d}",
                    object_name="next",
                    object_description="exact function named next",
                    payload={"fact_kind": "function"},
                )
                for index in range(40)
            ]
            facts.extend(
                _fact(
                    index + 100,
                    object_id=f"field:next:{index:02d}",
                    object_name="next",
                    object_description="linked-list next field",
                    payload={"fact_kind": "field", "owner_name": f"Node{index}"},
                )
                for index in range(12)
            )
            store.replace_facts(facts)

            results = store.search("next", limit=20)

            field_ids = [fact.object_id for fact in results if fact.object_id.startswith("field:next:")]
            self.assertGreaterEqual(len(field_ids), 3)
            self.assertIn("function:next:00", [fact.object_id for fact in results])

    def test_search_resolves_owner_qualified_field_when_common_name_is_crowded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = open_fact_store(Path(tmp), mode="w", log_enabled=False)
            facts = [
                _fact(
                    index,
                    object_id=f"global:value:{index:02d}",
                    object_name="value",
                    object_description="common global value",
                    payload={"fact_kind": "global"},
                )
                for index in range(30)
            ]
            facts.extend(
                [
                    _fact(
                        100,
                        object_id="field:JsonKeyValue:value",
                        object_name="value",
                        object_description="common json field",
                        payload={"fact_kind": "field", "owner_name": "JsonKeyValue"},
                    ),
                    _fact(
                        101,
                        object_id="field:NullableDatum:value",
                        object_name="value",
                        object_description="nullable payload slot",
                        payload={"fact_kind": "field", "owner_name": "NullableDatum"},
                    ),
                    _fact(
                        102,
                        object_id="field:JsonPathVariable:value",
                        object_name="value",
                        object_description="common json path field",
                        payload={"fact_kind": "field", "owner_name": "JsonPathVariable"},
                    ),
                ]
            )
            store.replace_facts(facts)

            dotted = [fact.object_id for fact in store.search("NullableDatum.value", limit=5)]
            spaced = [fact.object_id for fact in store.search("NullableDatum value", limit=5)]

            self.assertEqual(dotted[0], "field:NullableDatum:value")
            self.assertEqual(spaced[0], "field:NullableDatum:value")

    def test_first_query_uses_persistent_index_without_memory_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact(1)])
            reader = open_fact_store(target, mode="r", log_enabled=False)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("gzip data files must not be read during first query")

            with mock.patch.object(storage_module, "_iter_gzip_raw_lines", side_effect=forbidden):
                self.assertEqual([fact.object_id for fact in reader.search("alpha", limit=5)], ["fact:001"])


if __name__ == "__main__":
    unittest.main()

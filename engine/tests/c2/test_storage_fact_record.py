# Migrated from cipher-2 tests/test_storage_fact_record.py (M4 acceptance — imports rewritten cipher2.* -> arbiter_engine.facts.*).
import json
import unittest
from dataclasses import FrozenInstanceError

from arbiter_engine.facts.store import FactRecord, FactRelative, RelativeCondition, StorageError, StoredFactLine


class StorageFactRecordTest(unittest.TestCase):
    def test_fact_record_validates_required_fields_and_round_trips_payload(self):
        fact = FactRecord(
            object_id="fact:alpha",
            object_name="Alpha",
            object_description="Parses requests",
            object_source="src/alpha.py:1",
            object_profile="debug",
            object_caller="entry",
            object_callee="worker",
            payload={"fact_kind": "function", "rank": 1},
        )

        self.assertEqual(fact.object_id, "fact:alpha")
        self.assertEqual(fact.payload["fact_kind"], "function")
        encoded = json.dumps(fact.to_json(), sort_keys=True)
        self.assertEqual(FactRecord.from_json(json.loads(encoded)), fact)

    def test_fact_record_rejects_missing_or_invalid_required_fields(self):
        valid = {
            "object_id": "fact:alpha",
            "object_name": "Alpha",
            "object_description": "Parses requests",
            "object_source": "src/alpha.py:1",
            "object_profile": "debug",
        }
        cases = [
            {**valid, "object_id": ""},
            {**valid, "object_name": None},
            {**valid, "object_description": 3},
            {**valid, "object_source": ""},
            {**valid, "object_profile": ""},
            {**valid, "object_caller": 1},
            {**valid, "object_callee": 1},
        ]

        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(StorageError) as caught:
                    FactRecord(**kwargs)
                self.assertEqual(caught.exception.code, "invalid_fact")

    def test_fact_record_rejects_non_json_and_oversized_payload(self):
        with self.assertRaises(StorageError) as non_json:
            FactRecord(
                object_id="fact:bad",
                object_name="Bad",
                object_description="Bad payload",
                object_source="src/bad.py:1",
                object_profile="debug",
                payload={"bad": object()},
            )
        self.assertEqual(non_json.exception.code, "invalid_fact")

        with self.assertRaises(StorageError) as oversized:
            FactRecord(
                object_id="fact:large",
                object_name="Large",
                object_description="Large payload",
                object_source="src/large.py:1",
                object_profile="debug",
                payload={"large": "x" * 5000},
            )
        self.assertEqual(oversized.exception.code, "payload_too_large")

    def test_stored_fact_line_derives_fact_kind_and_payload_hash(self):
        fact = FactRecord(
            object_id="fact:alpha",
            object_name="Alpha",
            object_description="Parses requests",
            object_source="src/alpha.py:1",
            object_profile="debug",
            payload={"fact_kind": "function", "rank": 1},
        )

        line = StoredFactLine.from_fact(fact)
        decoded = StoredFactLine.from_json(line.to_json())

        self.assertEqual(line.schema_version, 5)
        self.assertEqual(line.object_id, "fact:alpha")
        self.assertEqual(line.fact_kind, "function")
        self.assertRegex(line.payload_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(decoded.to_fact(), fact)

    def test_stored_fact_line_defaults_fact_kind_to_fact(self):
        fact = FactRecord(
            object_id="fact:doc",
            object_name="Doc",
            object_description="Doc section",
            object_source="docs/a.md:L1-L2",
            object_profile="global",
            payload={"fact_kind": 12},
        )

        self.assertEqual(StoredFactLine.from_fact(fact).fact_kind, "fact")

    def test_hot_fact_and_relative_records_are_frozen_slotted(self):
        fact = FactRecord(
            object_id="fact:alpha",
            object_name="Alpha",
            object_description="Parses requests",
            object_source="src/alpha.py:1",
            object_profile="debug",
            payload={"fact_kind": "function"},
        )
        relative = FactRelative(
            relative_id="rel:alpha:beta",
            from_fact_id="fact:alpha",
            to_fact_id="fact:beta",
            relation_kind="direct_call",
            condition=RelativeCondition(kind="branch", expression="enabled", branch="then", source="src/alpha.py:1"),
            object_profile="debug",
            evidence_source="src/alpha.py:2",
            confidence=1.0,
            payload={"evidence": "call"},
        )

        self.assertFalse(hasattr(fact, "__dict__"))
        self.assertFalse(hasattr(relative, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            fact.object_name = "Changed"
        with self.assertRaises(FrozenInstanceError):
            relative.confidence = 0.5


if __name__ == "__main__":
    unittest.main()

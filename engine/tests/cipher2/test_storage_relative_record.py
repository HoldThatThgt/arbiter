# Migrated from cipher-2 tests/test_storage_relative_record.py (M4 acceptance — imports rewritten cipher2.*->arbiter_engine.facts.*, .cipher->.arbiter/facts).
import json
import unittest

from arbiter_engine.facts.store import FactRelative, RelativeCondition, StorageError, StoredRelativeLine


class StorageRelativeRecordTest(unittest.TestCase):
    def test_relative_condition_validates_and_round_trips(self):
        condition = RelativeCondition(kind="branch", expression="enabled", branch="then", source="src/a.c:10")

        encoded = json.dumps(condition.to_json(), sort_keys=True)

        self.assertEqual(RelativeCondition.from_json(json.loads(encoded)), condition)

    def test_fact_relative_validates_and_round_trips_payload(self):
        relative = FactRelative(
            relative_id="rel:assigned:1",
            from_fact_id="fact:slot:ops.read",
            to_fact_id="fact:function:my_read",
            relation_kind="assigned_to",
            condition=RelativeCondition(kind="branch", expression="a", branch="then", source="src/ops.c:12"),
            object_profile="debug",
            evidence_source="src/ops.c:13",
            confidence=1.0,
            payload={"assignment_kind": "field"},
        )

        encoded = json.dumps(relative.to_json(), sort_keys=True)

        self.assertEqual(FactRelative.from_json(json.loads(encoded)), relative)
        self.assertEqual(relative.to_json()["condition"]["branch"], "then")

    def test_stored_relative_line_hashes_and_restores_relative(self):
        relative = FactRelative(
            relative_id="rel:include:1",
            from_fact_id="fact:file:a",
            to_fact_id="fact:file:b",
            relation_kind="include",
            condition=None,
            object_profile="debug",
            evidence_source="src/a.c:1",
            confidence=1.0,
            payload={"spelling": "b.h"},
        )

        line = StoredRelativeLine.from_relative(relative)
        decoded = StoredRelativeLine.from_json(line.to_json())

        self.assertEqual(line.schema_version, 5)
        self.assertEqual(line.relative_id, "rel:include:1")
        self.assertEqual(line.condition, None)
        self.assertRegex(line.payload_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(decoded.to_relative(), relative)

    def test_fact_relative_rejects_invalid_fields(self):
        valid = {
            "relative_id": "rel:call:1",
            "from_fact_id": "fact:function:a",
            "to_fact_id": "fact:function:b",
            "relation_kind": "direct_call",
            "condition": None,
            "object_profile": "debug",
            "evidence_source": "src/a.c:20",
            "confidence": 1.0,
            "payload": {},
        }
        cases = [
            ({**valid, "relative_id": ""}, "invalid_relative"),
            ({**valid, "from_fact_id": ""}, "invalid_relative"),
            ({**valid, "to_fact_id": None}, "invalid_relative"),
            ({**valid, "object_profile": ""}, "invalid_relative"),
            ({**valid, "evidence_source": ""}, "invalid_relative"),
            ({**valid, "confidence": 1.1}, "invalid_relative"),
            ({**valid, "payload": []}, "invalid_relative"),
            ({**valid, "relation_kind": "include_fact"}, "invalid_relation_kind"),
        ]

        for kwargs, code in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(StorageError) as caught:
                    FactRelative(**kwargs)
                self.assertEqual(caught.exception.code, code)

    def test_field_access_relation_kinds_are_supported(self):
        for relation_kind in ("field_read", "field_write"):
            with self.subTest(relation_kind=relation_kind):
                relative = FactRelative(
                    relative_id=f"rel:{relation_kind}:1",
                    from_fact_id="fact:function:reader",
                    to_fact_id="fact:field:Context.member",
                    relation_kind=relation_kind,
                    condition=None,
                    object_profile="debug",
                    evidence_source="src/context.c:20",
                    confidence=1.0,
                    payload={"access_context": "argument"},
                )

                self.assertEqual(FactRelative.from_json(relative.to_json()), relative)

    def test_condition_rejects_invalid_shape_and_size(self):
        cases = [
            ({"kind": "if", "expression": "a"}, "invalid_condition"),
            ({"kind": "branch", "expression": 1}, "invalid_condition"),
            ({"kind": "branch", "branch": 1}, "invalid_condition"),
            ({"kind": "branch", "source": 1}, "invalid_condition"),
            ({"kind": "branch", "expression": "x" * 1200}, "condition_too_large"),
        ]

        for kwargs, code in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(StorageError) as caught:
                    RelativeCondition(**kwargs)
                self.assertEqual(caught.exception.code, code)

    def test_relative_rejects_non_json_and_oversized_payload(self):
        with self.assertRaises(StorageError) as non_json:
            FactRelative(
                relative_id="rel:bad",
                from_fact_id="fact:a",
                to_fact_id="fact:b",
                relation_kind="include",
                condition=None,
                object_profile="debug",
                evidence_source="src/a.c:1",
                confidence=1.0,
                payload={"bad": object()},
            )
        self.assertEqual(non_json.exception.code, "invalid_relative")

        with self.assertRaises(StorageError) as oversized:
            FactRelative(
                relative_id="rel:large",
                from_fact_id="fact:a",
                to_fact_id="fact:b",
                relation_kind="include",
                condition=None,
                object_profile="debug",
                evidence_source="src/a.c:1",
                confidence=1.0,
                payload={"large": "x" * 2500},
            )
        self.assertEqual(oversized.exception.code, "payload_too_large")


if __name__ == "__main__":
    unittest.main()

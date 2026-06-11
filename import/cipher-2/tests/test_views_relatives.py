import tempfile
import unittest
from pathlib import Path

from cipher2.storage import FactRecord, FactRelative, RelativeCondition, open_fact_store
from cipher2.tools.views import build_overview


def _fact(object_id: str) -> FactRecord:
    return FactRecord(
        object_id=object_id,
        object_name=object_id,
        object_description="relative view fact",
        object_source="src/main.c:1",
        object_profile="default",
        payload={"fact_kind": "function"},
    )


class ViewsRelativesTest(unittest.TestCase):
    def test_storage_model_exposes_relative_stats_and_relations_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            facts = [_fact("fact:caller"), _fact("fact:callee")]
            relative = FactRelative(
                relative_id="rel:call:1",
                from_fact_id="fact:caller",
                to_fact_id="fact:callee",
                relation_kind="direct_call",
                condition=RelativeCondition(kind="branch", branch="then", source="src/main.c:4"),
                object_profile="default",
                evidence_source="src/main.c:4",
                confidence=1.0,
            )
            field_read = FactRelative(
                relative_id="rel:field_read:1",
                from_fact_id="fact:caller",
                to_fact_id="fact:callee",
                relation_kind="field_read",
                condition=None,
                object_profile="default",
                evidence_source="src/main.c:8",
                confidence=1.0,
            )
            field_write = FactRelative(
                relative_id="rel:field_write:1",
                from_fact_id="fact:caller",
                to_fact_id="fact:callee",
                relation_kind="field_write",
                condition=None,
                object_profile="default",
                evidence_source="src/main.c:9",
                confidence=1.0,
            )
            store = open_fact_store(target, mode="w")
            store.replace_snapshot(facts, [relative, field_read, field_write])
            store.relatives_for_fact("fact:caller", direction="outgoing")

            overview = build_overview(target, include_sections=["storage"], top_n=5)

        self.assertEqual(overview.storage.total_relatives, 3)
        self.assertEqual(overview.storage.relation_kinds, {"direct_call": 1, "field_read": 1, "field_write": 1})
        self.assertEqual(overview.storage.field_read_count, 1)
        self.assertEqual(overview.storage.field_write_count, 1)
        self.assertEqual(overview.storage.conditional_relative_count, 1)
        self.assertEqual(overview.storage.orphan_relative_count, 0)
        self.assertEqual(overview.storage.relations_count, 1)


if __name__ == "__main__":
    unittest.main()

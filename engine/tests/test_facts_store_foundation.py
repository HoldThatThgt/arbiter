"""Phase 1.1 of the M4 facts absorption: the store's leaf layer (constants + utils),
ported near-verbatim from cipher-2 with the cipher2.common/cipher2.tools.log couplings
replaced by the local _common shim. This pins the data-contract constants and the
model-free helpers; the record models + query engine land in later increments."""

import unittest

from arbiter_engine.facts.store import constants, utils
from arbiter_engine.facts.store._common import JSONValue, open_log


class StoreConstantsTest(unittest.TestCase):
    def test_relation_vocabulary_is_intact(self):
        self.assertEqual(len(constants.RELATION_KINDS), 9)
        self.assertIn("direct_call", constants.RELATION_KINDS)
        self.assertIn("field_write", constants.RELATION_KINDS)
        # code ↔ kind is a stable bijection used by the SQLite read-index projection.
        for kind, code in constants.RELATION_KIND_CODES.items():
            self.assertEqual(constants.RELATION_KIND_BY_CODE[code], kind)
        self.assertEqual(
            set(constants.RELATION_KIND_CODES), constants.RELATION_KINDS
        )

    def test_payload_caps_and_relation_search_table(self):
        self.assertEqual(constants.MAX_FACT_PAYLOAD_BYTES, 4 * 1024)
        self.assertEqual(constants.MAX_RELATIVE_PAYLOAD_BYTES, 2 * 1024)
        self.assertEqual(constants.MAX_CONDITION_BYTES, 1024)
        # The six relation-search predicates the query grammar exposes.
        self.assertEqual(
            set(constants.RELATION_SEARCH_DEFINITIONS),
            {"readers", "writers", "accessors", "callers", "callees", "dispatches_via"},
        )
        self.assertEqual(
            constants.RELATION_SEARCH_DEFINITIONS["callers"],
            ("function", "incoming", ("direct_call",)),
        )

    def test_unsupported_relation_kind_message_guards_input(self):
        self.assertIn("bogus", constants.unsupported_relation_kind_message("bogus"))
        # Non-identifier input is not echoed back.
        self.assertNotIn("DROP TABLE", constants.unsupported_relation_kind_message("DROP TABLE"))


class StoreUtilsTest(unittest.TestCase):
    def test_canonical_json_is_sorted_and_compact(self):
        self.assertEqual(utils._canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_sha256_text_and_is_sha256(self):
        digest = utils._sha256_text("")
        self.assertEqual(
            digest, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        self.assertTrue(utils._is_sha256(digest))
        self.assertFalse(utils._is_sha256("not-a-hash"))

    def test_endpoint_source_file_parses_path_line(self):
        self.assertEqual(utils._endpoint_source_file("src/foo.c:42"), "src/foo.c")
        self.assertEqual(utils._endpoint_source_file("src/foo.c"), "src/foo.c")
        self.assertEqual(utils._endpoint_source_file(""), "<unknown-source>")

    def test_source_bucket(self):
        self.assertEqual(utils._source_bucket("src/foo.c:9"), "src/foo.c")
        self.assertEqual(utils._source_bucket("plain"), "plain")

    def test_compression_ratio_and_fixed_width_metadata(self):
        self.assertEqual(utils._compression_ratio(0, 0), 1.0)
        self.assertEqual(utils._compression_ratio(50, 100), 0.5)
        # Ratio fields render at fixed width so small snapshots don't oscillate.
        self.assertEqual(
            utils._canonical_metadata_value(7.8, key="compression_ratio"), "7.80"
        )

    def test_common_shim_log_is_noop(self):
        log = open_log()
        # The no-op sink must be safely usable as a context manager.
        with log as handle:
            handle.write("anything")
        self.assertIsNotNone(JSONValue)


if __name__ == "__main__":
    unittest.main()

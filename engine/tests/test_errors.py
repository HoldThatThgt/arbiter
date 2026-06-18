import unittest

from arbiter_engine import errors


class EngineErrorTaxonomyTest(unittest.TestCase):
    def test_spec_error_helpers_set_kind_and_required_fields(self):
        cases = [
            (errors.no_snapshot("run gear-up"), "no_snapshot", {"hint": "run gear-up"}),
            (
                errors.briefing_unresolved(["fact:missing"]),
                "briefing_unresolved",
                {"bad_refs": ["fact:missing"]},
            ),
            (errors.capability_revoked(), "capability_revoked", {}),
            (
                errors.recipe_pin_mismatch("abc", "def"),
                "recipe_pin_mismatch",
                {"expected": "abc", "found": "def"},
            ),
            (
                errors.engine_stale("1", "2"),
                "engine_stale",
                {"expected": "1", "found": "2"},
            ),
            (errors.harness_unavailable("gtest"), "harness_unavailable", {"harness": "gtest"}),
            (
                errors.indexer_unavailable("libclang_unavailable", "libclang library is unavailable"),
                "indexer_unavailable",
                {"toolchain_code": "libclang_unavailable", "detail": "libclang library is unavailable"},
            ),
            (errors.lock_timeout("state.lock"), "lock_timeout", {"lock": "state.lock"}),
        ]

        self.assertEqual(
            errors.SPEC_ERROR_KINDS,
            {
                "no_snapshot",
                "briefing_unresolved",
                "capability_revoked",
                "recipe_pin_mismatch",
                "engine_stale",
                "harness_unavailable",
                "indexer_unavailable",
                "lock_timeout",
            },
        )
        for err, kind, fields in cases:
            with self.subTest(kind=kind):
                self.assertEqual(err.data["kind"], kind)
                for field, value in fields.items():
                    self.assertEqual(err.data[field], value)

    def test_unknown_error_kind_requires_taxonomy_update(self):
        with self.assertRaises(ValueError):
            errors.RPCError(-32000, "new kind", {"kind": "not_documented"})

    def test_internal_error_is_a_validated_chassis_kind(self):
        self.assertIn("internal_error", errors.CHASSIS_ERROR_KINDS)
        self.assertIn("internal_error", errors.KNOWN_ERROR_KINDS)

        err = errors.internal_error(KeyError("missing-thing"))

        self.assertEqual(err.code, -32603)
        self.assertEqual(err.data["kind"], "internal_error")
        self.assertEqual(err.data["exception"], "KeyError")
        self.assertIn("missing-thing", err.data["detail"])


if __name__ == "__main__":
    unittest.main()

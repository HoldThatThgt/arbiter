import importlib
import unittest


EXPECTED_TESTS = {
    "manifest and snapshot lock": [
        "tests.test_retrieval_harness.RetrievalHarnessTest.test_manifest_loads_repo_spec_and_validates_locked_snapshot",
        "tests.test_retrieval_harness.RetrievalHarnessTest.test_snapshot_mismatch_skips_repo_without_rebuild",
    ],
    "preview full scoring": [
        "tests.test_retrieval_harness.RetrievalHarnessTest.test_probe_reports_preview_full_and_bound_loss_from_mcp_and_store",
        "tests.test_retrieval_harness.RetrievalHarnessTest.test_high_fan_in_field_bounded_miss_reuses_preview_partial_root_cause",
        "tests.test_retrieval_harness.RetrievalHarnessTest.test_full_answers_use_store_ceiling_not_mcp_budget",
        "tests.test_retrieval_harness.RetrievalHarnessTest.test_gold_graph_generates_unbiased_callers_case",
    ],
    "manual command surface": [
        "tests.test_retrieval_harness.RetrievalHarnessTest.test_run_entrypoint_writes_json_and_markdown_outputs",
        "tests.test_retrieval_harness.RetrievalHarnessTest.test_invalid_manifest_is_reported_by_run_entrypoint",
    ],
    "retest probe metrics and report output": [
        "tests.test_retrieval_retest_harness.RetrievalRetestHarnessTest.test_probe_mode_reports_preview_full_gap_and_outputs_files",
    ],
    "weak model adapter protocol": [
        "tests.test_retrieval_retest_harness.RetrievalRetestHarnessTest.test_external_adapter_protocol_scores_grep_vs_cipher_conditions",
        "tests.test_retrieval_retest_harness.RetrievalRetestHarnessTest.test_missing_adapter_environment_marks_ab_skipped",
    ],
    "retest manifest validation": [
        "tests.test_retrieval_retest_harness.RetrievalRetestHarnessTest.test_manifest_validation_rejects_missing_libraries",
    ],
}


class RetrievalBenchmarkCoverageMatrixTest(unittest.TestCase):
    def test_retrieval_benchmark_matrix_has_behavior_tests_for_every_requirement(self):
        missing = []
        for requirement, test_names in EXPECTED_TESTS.items():
            self.assertGreater(len(test_names), 0, requirement)
            for test_name in test_names:
                if not self._test_exists(test_name):
                    missing.append(f"{requirement}: {test_name}")

        self.assertEqual(missing, [])

    def _test_exists(self, test_name):
        module_name, class_name, method_name = test_name.rsplit(".", 2)
        module = importlib.import_module(module_name)
        test_class = getattr(module, class_name, None)
        return test_class is not None and callable(getattr(test_class, method_name, None))


if __name__ == "__main__":
    unittest.main()

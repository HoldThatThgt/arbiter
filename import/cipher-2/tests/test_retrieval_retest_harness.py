import json
import sys
import tempfile
import unittest
from pathlib import Path

from benchmarks.retrieval.manifest import load_manifest
from benchmarks.retrieval.models import (
    BaselineMetric,
    EvalCase,
    LibraryPlan,
    ModelPlan,
    RetrievalBenchmarkError,
    RetestManifest,
)
from benchmarks.retrieval.run import run_manifest
from cipher2.storage import FactRecord, FactRelative, open_fact_store


def _fact(object_id: str, name: str) -> FactRecord:
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=f"{name} function",
        object_source="src/main.c:1",
        object_profile="default",
        payload={"fact_kind": "function"},
    )


def _relative(relative_id: str, from_id: str, to_id: str) -> FactRelative:
    return FactRelative(
        relative_id=relative_id,
        from_fact_id=from_id,
        to_fact_id=to_id,
        relation_kind="direct_call",
        condition=None,
        object_profile="default",
        evidence_source="src/main.c:8",
        confidence=1.0,
        payload={"line": 8},
    )


def _target_repo(root: Path) -> Path:
    target = root / "target"
    target.mkdir()
    open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
        [
            _fact("fact:add_var", "add_var"),
            _fact("fact:div_mod_var", "div_mod_var"),
        ],
        [_relative("rel:call", "fact:div_mod_var", "fact:add_var")],
    )
    return target


def _manifest(target: Path, model_plan=None) -> RetestManifest:
    case = EvalCase(
        case_id="smoke-callers",
        library="smoke",
        dimension="CALLERS",
        query="add_var",
        question="Who calls add_var?",
        gold_answers=["div_mod_var"],
        target_fact_id="fact:add_var",
        grep_context=["grep found add_var definition"],
    )
    return RetestManifest(
        libraries=[LibraryPlan(name="smoke", repo=str(target), snapshot_id="current", cases=[case])],
        seed=7,
        clang16_gold_version="LLVM Clang 16.0.6",
        baselines=[BaselineMetric(library="smoke", dimension="CALLERS", preview_before=0.16, full_before=0.83)],
        model_plan=model_plan,
    )


class RetrievalRetestHarnessTest(unittest.TestCase):
    def test_probe_mode_reports_preview_full_gap_and_outputs_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _target_repo(Path(tmp))
            output = Path(tmp) / "report.md"

            summary = run_manifest(_manifest(target), mode="probe", output=output)

            callers = next(metric for metric in summary.retrieval if metric.dimension == "CALLERS")
            self.assertEqual(callers.case_count, 1)
            self.assertEqual(callers.recover_preview, 1.0)
            self.assertEqual(callers.recover_full, 1.0)
            self.assertEqual(callers.preview_gap, 0.0)
            self.assertAlmostEqual(callers.ceiling_delta, 0.17)
            self.assertTrue(output.exists())
            self.assertTrue(output.with_suffix(".json").exists())
            self.assertIn("recover@preview", output.read_text(encoding="utf-8"))

    def test_external_adapter_protocol_scores_grep_vs_cipher_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = _target_repo(root)
            adapter = root / "adapter.py"
            adapter.write_text(
                "import json, sys\n"
                "request = json.loads(sys.stdin.read())\n"
                "answer = ['div_mod_var'] if request['condition'] == 'grep_cipher' else ['wrong']\n"
                "print(json.dumps({'case_id': request['case_id'], 'condition': request['condition'], 'answer_names': answer, 'raw_answer': ','.join(answer)}))\n",
                encoding="utf-8",
            )
            plan = ModelPlan(
                enabled=True,
                adapter_kind="external_command",
                command=[sys.executable, str(adapter)],
                required_env=[],
                model_label="fake-weak",
                timeout_seconds=5,
            )

            summary = run_manifest(_manifest(target, model_plan=plan), mode="all")

            callers = next(metric for metric in summary.weak_model_ab if metric.dimension == "CALLERS")
            self.assertEqual(callers.acc_b, 0.0)
            self.assertEqual(callers.acc_c, 1.0)
            self.assertEqual(callers.delta, 1.0)
            self.assertEqual(callers.rescue, 1.0)

    def test_missing_adapter_environment_marks_ab_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _target_repo(Path(tmp))
            plan = ModelPlan(
                enabled=True,
                adapter_kind="external_command",
                command=[sys.executable, "-c", "print('{}')"],
                required_env=["CIPHER2_RETRIEVAL_TEST_MISSING_ENV"],
                model_label="fake-weak",
                timeout_seconds=5,
            )

            summary = run_manifest(_manifest(target, model_plan=plan), mode="ab")

            self.assertEqual(summary.weak_model_ab, [])
            self.assertEqual(summary.skipped[0]["reason"], "missing_model_env")

    def test_manifest_validation_rejects_missing_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text(json.dumps({"seed": 1, "clang16_gold_version": "LLVM Clang 16.0.6"}), encoding="utf-8")

            with self.assertRaises(RetrievalBenchmarkError):
                load_manifest(manifest)


if __name__ == "__main__":
    unittest.main()

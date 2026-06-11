import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from benchmarks.retrieval.coverage_pool import full_answers_for_case, validate_snapshot
from benchmarks.retrieval.genq import cases_from_gold
from benchmarks.retrieval.manifest import load_manifest
from benchmarks.retrieval.models import GoldCall, GoldGraph
from benchmarks.retrieval.retrieval_probe import run_probe
from benchmarks.retrieval.run import main as run_main
from cipher2.storage import FactRecord, FactRelative, open_fact_store


def _fact(object_id, name, *, description="function", source="src/main.c:1", kind="function"):
    return FactRecord(
        object_id=object_id,
        object_name=name,
        object_description=description,
        object_source=source,
        object_profile=f"{kind}:{name}",
        object_caller=None,
        object_callee=None,
        payload={"fact_kind": kind},
    )


def _relative(relative_id, from_fact_id, to_fact_id, kind="direct_call"):
    return FactRelative(
        relative_id=relative_id,
        from_fact_id=from_fact_id,
        to_fact_id=to_fact_id,
        relation_kind=kind,
        condition=None,
        object_profile=f"{kind} edge",
        evidence_source="src/main.c:10",
        confidence=1.0,
        payload={"source": "fixture"},
    )


class RetrievalHarnessTest(unittest.TestCase):
    def test_manifest_loads_repo_spec_and_validates_locked_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, manifest_path, snapshot_id = self._write_smoke_repo(Path(tmp))

            manifest = load_manifest(manifest_path)
            validation = validate_snapshot(manifest.repositories[0])

        self.assertEqual(manifest.seed, 7)
        self.assertEqual(manifest.repositories[0].name, "smoke")
        self.assertEqual(manifest.repositories[0].snapshot_id, snapshot_id)
        self.assertTrue(validation.ok)
        self.assertIsNone(validation.reason)

    def test_probe_reports_preview_full_and_bound_loss_from_mcp_and_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            _target, manifest_path, _snapshot_id = self._write_smoke_repo(Path(tmp))
            manifest = load_manifest(manifest_path)

            summary = run_probe(manifest, budget="normal")

        metric = next(item for item in summary.metrics if item.library == "smoke" and item.dimension == "CALLERS")
        self.assertEqual(metric.case_count, 1)
        self.assertEqual(metric.recover_preview, 1.0)
        self.assertEqual(metric.recover_full, 1.0)
        self.assertEqual(metric.bound_loss, 0.0)
        self.assertEqual(summary.cases[0].root_cause, "recovered")
        self.assertIn("div_mod_var", summary.cases[0].preview_answers)
        self.assertEqual(summary.coverage[0].covered_count, 1)
        self.assertEqual(summary.coverage[0].precision, 1.0)

    def test_high_fan_in_field_bounded_miss_reuses_preview_partial_root_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            _target, manifest_path, _snapshot_id = self._write_high_fan_in_field_repo(Path(tmp))
            manifest = load_manifest(manifest_path)

            summary = run_probe(manifest, budget="normal")

        metric = next(item for item in summary.metrics if item.library == "field-smoke" and item.dimension == "FIELD_ACC")
        case = summary.cases[0]
        self.assertEqual(case.root_cause, "preview_partial")
        self.assertEqual(case.preview_recovered, 0.5)
        self.assertEqual(case.full_recovered, 1.0)
        self.assertEqual(metric.bound_loss, 0.5)
        self.assertIn("reader_00", case.preview_answers)
        self.assertNotIn("reader_29", case.preview_answers)
        self.assertIn("reader_29", case.full_answers)

    def test_full_answers_use_store_ceiling_not_mcp_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, manifest_path, _snapshot_id = self._write_smoke_repo(Path(tmp))
            case = load_manifest(manifest_path).repositories[0].cases[0]

            answers = full_answers_for_case(target, case)

        self.assertIn("div_mod_var", answers)
        self.assertIn("fact:function:div_mod_var", answers)

    def test_gold_graph_generates_unbiased_callers_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, _manifest_path, _snapshot_id = self._write_smoke_repo(Path(tmp))
            gold = GoldGraph(calls=[GoldCall(caller="div_mod_var", callee="add_var", source="src/numeric.c:30")])

            cases = cases_from_gold(library="smoke", repo_root=target, gold=gold, dimensions=["CALLERS"])

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].dimension, "CALLERS")
        self.assertEqual(cases[0].target_fact_id, "fact:function:add_var")
        self.assertEqual(cases[0].gold_answers, ["div_mod_var"])

    def test_snapshot_mismatch_skips_repo_without_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            _target, manifest_path, _snapshot_id = self._write_smoke_repo(Path(tmp), snapshot_id_override="sha256-wrong")
            manifest = load_manifest(manifest_path)

            summary = run_probe(manifest, budget="normal")

        self.assertEqual(summary.skipped, [{"library": "smoke", "reason": "snapshot_mismatch"}])
        self.assertTrue(any(metric.skip_reason == "snapshot_mismatch" for metric in summary.metrics))

    def test_run_entrypoint_writes_json_and_markdown_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            _target, manifest_path, _snapshot_id = self._write_smoke_repo(Path(tmp))
            output = Path(tmp) / "out"

            exit_code = run_main(["--manifest", str(manifest_path), "--output", str(output)])

            payload = json.loads((output / "run_summary.json").read_text(encoding="utf-8"))
            report = (output / "report.md").read_text(encoding="utf-8")
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["metrics"][0]["library"], "smoke")
        self.assertEqual(payload["coverage"][0]["covered_count"], 1)
        self.assertIn("recover@preview", report)
        self.assertIn("## Coverage", report)

    def test_invalid_manifest_is_reported_by_run_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            output = Path(tmp) / "out"

            with redirect_stderr(StringIO()):
                exit_code = run_main(["--manifest", str(manifest_path), "--output", str(output)])

        self.assertEqual(exit_code, 2)

    def _write_smoke_repo(self, root, snapshot_id_override=None):
        target = root / "target"
        target.mkdir()
        manifest = open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(
            [
                _fact("fact:function:add_var", "add_var", description="target function", source="src/numeric.c:10"),
                _fact("fact:function:div_mod_var", "div_mod_var", description="caller function", source="src/numeric.c:30"),
            ],
            [_relative("rel:div_mod_var:add_var", "fact:function:div_mod_var", "fact:function:add_var")],
        )
        expected_snapshot_id = snapshot_id_override or manifest.snapshot_id
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "clang_executable": "/usr/bin/clang-16",
                    "seed": 7,
                    "dimensions": ["CALLERS"],
                    "case_limit": 10,
                    "repositories": [
                        {
                            "name": "smoke",
                            "repo_root": str(target),
                            "snapshot_id": expected_snapshot_id,
                            "snapshot_path": f".cipher/snapshots/{manifest.snapshot_id}",
                            "clang16_version": "LLVM Clang 16.0.6",
                            "cases": [
                                {
                                    "case_id": "smoke-callers",
                                    "dimension": "CALLERS",
                                    "query": "add_var",
                                    "target_fact_id": "fact:function:add_var",
                                    "gold_answers": ["div_mod_var"],
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return target, manifest_path, manifest.snapshot_id

    def _write_high_fan_in_field_repo(self, root):
        target = root / "target"
        target.mkdir()
        field_id = "fact:field:list:length"
        facts = [
            _fact(
                field_id,
                "length",
                description="List length field",
                source="src/list.h:12",
                kind="field",
            )
        ]
        facts.extend(
            _fact(
                f"fact:function:reader_{index:02d}",
                f"reader_{index:02d}",
                description="field reader function",
                source=f"src/readers.c:{index + 1}",
            )
            for index in range(30)
        )
        relatives = [
            _relative(
                f"rel:field-read:{index:02d}",
                f"fact:function:reader_{index:02d}",
                field_id,
                kind="field_read",
            )
            for index in range(30)
        ]
        manifest = open_fact_store(target, mode="w", log_enabled=False).replace_snapshot(facts, relatives)
        manifest_path = root / "field_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "clang_executable": "/usr/bin/clang-16",
                    "seed": 7,
                    "dimensions": ["FIELD_ACC"],
                    "case_limit": 10,
                    "repositories": [
                        {
                            "name": "field-smoke",
                            "repo_root": str(target),
                            "snapshot_id": manifest.snapshot_id,
                            "snapshot_path": f".cipher/snapshots/{manifest.snapshot_id}",
                            "clang16_version": "LLVM Clang 16.0.6",
                            "cases": [
                                {
                                    "case_id": "field-high-fan-in",
                                    "dimension": "FIELD_ACC",
                                    "query": "length",
                                    "target_fact_id": field_id,
                                    "gold_answers": ["reader_00", "reader_29"],
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return target, manifest_path, manifest.snapshot_id


if __name__ == "__main__":
    unittest.main()

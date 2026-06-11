import json
import os
import shlex
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cipher2.config import load_config
from cipher2.initializer.extractor import code as code_extractor
from cipher2.initializer.extractor.code import CodeFactExtractor
from cipher2.tools.log import open_log
from tests.toolchain_helpers import write_fake_toolchain


class CodeExtractorParallelTest(unittest.TestCase):
    def test_parallel_workers_merge_out_of_order_results_by_source_path_and_resolve_calls(self):
        self._skip_if_managed_worker_unavailable()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_parallel_fixture(target)
            serial = CodeFactExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 1}}, observe=False),
                log_enabled=True,
            ).collect(["src"], "debug")

            parallel = CodeFactExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 2}}, observe=False),
                log_enabled=True,
            ).collect(["src"], "debug")
            events = open_log(target).read_events(channel="initializer").events

        self.assertEqual(_result_signature(parallel), _result_signature(serial))
        self.assertEqual(sorted(fact.object_name for fact in parallel.facts if fact.fact_kind == "function"), ["entry", "helper"])
        self.assertEqual(sorted(item.rel_path for item in parallel.source_inventory), ["src/a.c", "src/b.c"])
        direct_calls = [relative for relative in parallel.relatives if relative.relation_kind == "direct_call"]
        self.assertEqual(len(direct_calls), 1)
        self.assertEqual(direct_calls[0].payload["resolution_strategy"], "unique_name")
        worker_event = [event for event in events if event.event_name == "extractor.code.worker_pool"][-1]
        self.assertEqual(worker_event.status, "ok")
        self.assertEqual(worker_event.counts["worker_count"], 2)
        self.assertEqual(worker_event.counts["successful_file_count"], 2)
        self.assertEqual(worker_event.counts["skipped_file_count"], 0)
        self.assertEqual(worker_event.counts["map_output_segment_count"], 6)
        self.assertGreater(worker_event.counts["map_output_bytes"], 0)
        self.assertEqual(worker_event.counts["stale_run_gc_count"], 0)
        self.assertIn("relative_map_input_count", worker_event.counts)
        self.assertIn("relative_map_written_count", worker_event.counts)
        self.assertIn("relative_map_skipped_exact_count", worker_event.counts)
        self.assertIn("relative_worker_dedup_tracked_entry_count", worker_event.counts)
        self.assertEqual(worker_event.payload["mode"], "bounded_pool")
        resolver_event = [event for event in events if event.event_name == "extractor.code.direct_call_resolution"][-1]
        self.assertEqual(resolver_event.counts["pending_shard_count"], 1)
        self.assertEqual(resolver_event.counts["function_index_entry_count"], 2)
        self.assertEqual(resolver_event.counts["resolver_worker_count"], 1)

    def test_parallel_workers_keep_recoverable_file_errors_isolated(self):
        self._skip_if_managed_worker_unavailable()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_parallel_fixture(target)
            _wrap_clang_to_fail_source(target, "src/a.c")

            result = CodeFactExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 2}}, observe=False),
                log_enabled=True,
            ).collect(["src"], "debug")
            events = open_log(target).read_events(channel="initializer").events

        self.assertEqual([fact.object_name for fact in result.facts if fact.fact_kind == "function"], ["helper"])
        self.assertEqual([(error.code, error.source) for error in result.errors], [("clang_ast_failed", "src/a.c")])
        self.assertEqual([item.rel_path for item in result.source_inventory], ["src/b.c"])
        warning_event = next(event for event in events if event.event_name == "extractor.code.file" and event.status == "warning")
        self.assertEqual(warning_event.payload["diagnostic_kind"], "fatal")
        worker_event = next(event for event in events if event.event_name == "extractor.code.worker_pool")
        self.assertEqual(worker_event.status, "warning")
        self.assertEqual(worker_event.counts["worker_count"], 2)
        self.assertEqual(worker_event.counts["successful_file_count"], 1)
        self.assertEqual(worker_event.counts["skipped_file_count"], 1)
        self.assertEqual(worker_event.counts["warning_count"], 1)

    def test_managed_worker_timeout_records_warning_and_continues_other_sources(self):
        self._skip_if_fork_managed_worker_unavailable()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source_a = target / "src" / "a.c"
            source_b = target / "src" / "b.c"
            _write_source(source_a, "int stuck(void) { return 0; }\n")
            _write_source(source_b, "int helper(void) { return 1; }\n")
            ast_by_rel = {"src/b.c": _function_translation_unit(source_b, "helper")}
            original_worker = code_extractor._run_file_work_item_in_process

            def slow_worker(item):
                if item.rel_source == "src/a.c":
                    time.sleep(5)
                return original_worker(item)

            def timeout_for(path):
                return 0 if Path(path).name == "a.c" else 30

            with mock.patch.object(code_extractor, "_run_file_work_item_in_process", slow_worker), mock.patch.object(
                code_extractor,
                "_ast_command_timeout_seconds",
                side_effect=timeout_for,
            ):
                result = _SyntheticAstExtractor(
                    target,
                    load_config(target, overrides={"extractor": {"worker_count": 2}}, observe=False),
                    ast_by_rel,
                    log_enabled=True,
                ).collect(["src"], "debug")
            events = open_log(target).read_events(channel="initializer").events

        self.assertEqual([fact.object_name for fact in result.facts if fact.fact_kind == "function"], ["helper"])
        self.assertEqual([(error.code, error.source) for error in result.errors], [("clang_ast_failed", "src/a.c")])
        self.assertEqual(result.errors[0].details["diagnostic_kind"], "timeout")
        self.assertEqual(result.errors[0].details["reason"], "timeout")
        self.assertEqual(result.errors[0].details["timeout_seconds"], 0)
        self.assertEqual([item.rel_path for item in result.source_inventory], ["src/b.c"])
        warning_event = next(event for event in events if event.event_name == "extractor.code.file" and event.status == "warning")
        self.assertEqual(warning_event.payload["diagnostic_kind"], "timeout")
        self.assertEqual(warning_event.payload["diagnostic_reason"], "timeout")
        self.assertEqual(warning_event.payload["timeout_seconds"], 0)
        worker_event = next(event for event in events if event.event_name == "extractor.code.worker_pool")
        self.assertEqual(worker_event.status, "warning")
        self.assertEqual(worker_event.counts["worker_count"], 2)
        self.assertEqual(worker_event.counts["successful_file_count"], 1)
        self.assertEqual(worker_event.counts["skipped_file_count"], 1)
        self.assertEqual(worker_event.counts["worker_timeout_count"], 1)
        self.assertEqual(worker_event.counts["worker_restart_count"], 1)
        self.assertEqual(worker_event.counts["worker_crash_count"], 0)

    def test_serial_managed_worker_timeout_restarts_and_continues_next_source(self):
        self._skip_if_fork_managed_worker_unavailable()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source_a = target / "src" / "a.c"
            source_b = target / "src" / "b.c"
            _write_source(source_a, "int stuck(void) { return 0; }\n")
            _write_source(source_b, "int helper(void) { return 1; }\n")
            ast_by_rel = {"src/b.c": _function_translation_unit(source_b, "helper")}
            original_worker = code_extractor._run_file_work_item_in_process

            def slow_worker(item):
                if item.rel_source == "src/a.c":
                    time.sleep(5)
                return original_worker(item)

            def timeout_for(path):
                return 0 if Path(path).name == "a.c" else 30

            with mock.patch.object(code_extractor, "_run_file_work_item_in_process", slow_worker), mock.patch.object(
                code_extractor,
                "_ast_command_timeout_seconds",
                side_effect=timeout_for,
            ):
                result = _SyntheticAstExtractor(
                    target,
                    load_config(target, overrides={"extractor": {"worker_count": 1}}, observe=False),
                    ast_by_rel,
                    log_enabled=True,
                ).collect(["src"], "debug")
            events = open_log(target).read_events(channel="initializer").events

        self.assertEqual([fact.object_name for fact in result.facts if fact.fact_kind == "function"], ["helper"])
        self.assertEqual([(error.code, error.source) for error in result.errors], [("clang_ast_failed", "src/a.c")])
        self.assertEqual(result.errors[0].details["diagnostic_kind"], "timeout")
        self.assertEqual(result.errors[0].details["timeout_seconds"], 0)
        self.assertEqual([item.rel_path for item in result.source_inventory], ["src/b.c"])
        worker_event = next(event for event in events if event.event_name == "extractor.code.worker_pool")
        self.assertEqual(worker_event.payload["mode"], "serial")
        self.assertEqual(worker_event.counts["worker_count"], 1)
        self.assertEqual(worker_event.counts["successful_file_count"], 1)
        self.assertEqual(worker_event.counts["skipped_file_count"], 1)
        self.assertEqual(worker_event.counts["worker_timeout_count"], 1)
        self.assertEqual(worker_event.counts["worker_restart_count"], 1)
        self.assertEqual(worker_event.counts["worker_crash_count"], 0)

    def test_managed_worker_crash_records_warning_and_restarts(self):
        self._skip_if_fork_managed_worker_unavailable()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source_a = target / "src" / "a.c"
            source_b = target / "src" / "b.c"
            _write_source(source_a, "int crash_me(void) { return 0; }\n")
            _write_source(source_b, "int helper(void) { return 1; }\n")
            ast_by_rel = {"src/b.c": _function_translation_unit(source_b, "helper")}
            original_worker = code_extractor._run_file_work_item_in_process

            def crashing_worker(item):
                if item.rel_source == "src/a.c":
                    os._exit(7)
                return original_worker(item)

            with mock.patch.object(code_extractor, "_run_file_work_item_in_process", crashing_worker):
                result = _SyntheticAstExtractor(
                    target,
                    load_config(target, overrides={"extractor": {"worker_count": 1}}, observe=False),
                    ast_by_rel,
                    log_enabled=True,
                ).collect(["src"], "debug")
            events = open_log(target).read_events(channel="initializer").events

        self.assertEqual([fact.object_name for fact in result.facts if fact.fact_kind == "function"], ["helper"])
        self.assertEqual([(error.code, error.source) for error in result.errors], [("clang_ast_failed", "src/a.c")])
        self.assertEqual(result.errors[0].details["diagnostic_kind"], "unknown")
        self.assertEqual(result.errors[0].details["reason"], "worker_crash")
        self.assertEqual(result.errors[0].details["worker_exitcode"], 7)
        self.assertEqual([item.rel_path for item in result.source_inventory], ["src/b.c"])
        warning_event = next(event for event in events if event.event_name == "extractor.code.file" and event.status == "warning")
        self.assertEqual(warning_event.payload["diagnostic_kind"], "unknown")
        self.assertEqual(warning_event.payload["diagnostic_reason"], "worker_crash")
        self.assertEqual(warning_event.payload["worker_exitcode"], 7)
        worker_event = next(event for event in events if event.event_name == "extractor.code.worker_pool")
        self.assertEqual(worker_event.status, "warning")
        self.assertEqual(worker_event.counts["worker_count"], 1)
        self.assertEqual(worker_event.counts["successful_file_count"], 1)
        self.assertEqual(worker_event.counts["skipped_file_count"], 1)
        self.assertEqual(worker_event.counts["worker_timeout_count"], 0)
        self.assertEqual(worker_event.counts["worker_restart_count"], 1)
        self.assertEqual(worker_event.counts["worker_crash_count"], 1)

    def test_parallel_workers_merge_header_globals_by_symbol_source_and_linkage(self):
        self._skip_if_managed_worker_unavailable()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "hooks.h"
            source_a = target / "src" / "a.c"
            source_b = target / "src" / "b.c"
            _write_source(header, "extern int (*get_attavgwidth_hook)(void);\n")
            _write_source(source_a, '#include "../include/hooks.h"\n')
            _write_source(source_b, '#include "../include/hooks.h"\n')
            header_file = header.as_posix()
            ast_by_rel = {
                "src/a.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [_header_global_decl(header_file)],
                },
                "src/b.c": {
                    "kind": "TranslationUnitDecl",
                    "inner": [
                        {
                            "kind": "MacroDefinitionRecord",
                            "name": "B_BEFORE_HOOK",
                            "loc": _loc(1, source_b.as_posix()),
                        },
                        _header_global_decl(header_file),
                    ],
                },
            }

            result = _SyntheticAstExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 2}}, observe=False),
                ast_by_rel,
            ).collect(["src"], "debug")

        hooks = [
            fact
            for fact in result.facts
            if fact.fact_kind == "global" and fact.object_name == "get_attavgwidth_hook"
        ]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0].payload["canonical_source"], "include/hooks.h")
        self.assertEqual(hooks[0].payload["linkage"], "extern")

    def test_map_reduce_staging_gc_removes_only_stale_initializer_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_parallel_fixture(target)
            stale_run = target / ".cipher" / "run" / "initializer-mapreduce" / "stale-run"
            stale_run.mkdir(parents=True)
            (stale_run / "segment.jsonl").write_text("{}\n", encoding="utf-8")
            os.utime(stale_run, (1, 1))
            incremental = target / ".cipher" / "run" / "incremental" / "stale-run"
            incremental.mkdir(parents=True)
            (incremental / "overlay.json").write_text("{}\n", encoding="utf-8")

            CodeFactExtractor(
                target,
                load_config(target, overrides={"extractor": {"worker_count": 1}}, observe=False),
                log_enabled=True,
            ).collect(["src"], "debug")
            events = open_log(target).read_events(channel="initializer").events

            worker_event = [event for event in events if event.event_name == "extractor.code.worker_pool"][-1]
            self.assertEqual(worker_event.counts["stale_run_gc_count"], 1)
            self.assertFalse(stale_run.exists())
            self.assertTrue(incremental.exists())

    def _skip_if_managed_worker_unavailable(self):
        context = code_extractor._multiprocessing_context()
        try:
            process = context.Process(target=_managed_worker_smoke)
            process.start()
        except OSError as exc:
            self.skipTest(f"managed worker process unavailable: {exc}")
        process.join(timeout=1.0)
        if process.exitcode is None:
            process.terminate()
            process.join(timeout=1.0)
            self.skipTest("managed worker process did not exit")
        if process.exitcode != 0:
            self.skipTest(f"managed worker process exited with {process.exitcode}")

    def _skip_if_fork_managed_worker_unavailable(self):
        context = code_extractor._multiprocessing_context()
        if context.get_start_method() != "fork":
            self.skipTest("managed worker patch inheritance requires fork start method")


def _result_signature(result):
    return {
        "facts": sorted(json.dumps(fact.to_fact_record().to_json(), sort_keys=True) for fact in result.facts),
        "relatives": sorted(json.dumps(relative.to_json(), sort_keys=True) for relative in result.relatives),
        "source_inventory": sorted(json.dumps(entry.to_json(), sort_keys=True) for entry in result.source_inventory),
        "errors": [(error.code, error.source) for error in result.errors],
    }


def _managed_worker_smoke():
    return None


def _write_parallel_fixture(target: Path) -> None:
    _write_source(target / "src" / "a.c", "int entry(void) { return helper(); }\n")
    _write_source(target / "src" / "b.c", "int helper(void) { return 1; }\n")
    write_fake_toolchain(target)


def _write_source(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _loc(line: int, file: str):
    return {"line": line, "file": file}


def _qtype(text: str):
    return {"qualType": text}


def _header_global_decl(header_file: str):
    return {
        "kind": "VarDecl",
        "name": "get_attavgwidth_hook",
        "loc": _loc(1, header_file),
        "type": _qtype("int (*)(void)"),
        "storageClass": "extern",
    }


def _function_translation_unit(source: Path, name: str):
    return {
        "kind": "TranslationUnitDecl",
        "inner": [
            {
                "kind": "FunctionDecl",
                "name": name,
                "loc": _loc(1, source.as_posix()),
                "type": _qtype("int (void)"),
                "isThisDeclarationADefinition": True,
                "inner": [{"kind": "CompoundStmt", "inner": []}],
            }
        ],
    }


class _SyntheticAstExtractor(CodeFactExtractor):
    def __init__(self, target_repo: Path, config, ast, *, log_enabled: bool = False):
        super().__init__(target_repo, config, log_enabled=log_enabled)
        self._ast = ast

    def _validate_toolchain(self) -> None:
        self.toolchain_probe_result = code_extractor.ToolchainProbeResult(
            clang_executable="synthetic-clang",
            clang_vendor="llvm",
            clang_version="16.0.0",
            ast_json_supported=False,
            type_driven_ast=True,
            loc_file_supported=True,
            call_reference_supported=True,
            member_reference_supported=True,
            qual_type_supported=True,
            ast_root_kind="TranslationUnitDecl",
            gcc_required=False,
            gcc_checked=False,
            backend="libclang",
            libclang_library="synthetic-libclang",
            libclang_library_scope="test",
            libclang_version="16.0.0",
            version_match=True,
        )
        self._ast_backend = _SyntheticAstBackend(self._ast)


class _SyntheticAstBackend(code_extractor._AstBackend):
    backend_name = "libclang"

    def __init__(self, ast):
        self._ast = ast

    def probe(self):
        raise AssertionError("synthetic backend is installed after probe")

    def load_ast(self, path: Path, rel_source: str, compile_lookup=None):
        if isinstance(self._ast, dict) and rel_source in self._ast:
            return code_extractor._AstLoadResult(ast=self._ast[rel_source])
        return code_extractor._AstLoadResult(ast=self._ast)


def _wrap_clang_to_fail_source(target: Path, rel_source: str) -> None:
    clang = target / "bin" / "clang"
    real_clang = target / "bin" / "clang-real"
    clang.rename(real_clang)
    clang.write_text(
        "#!/bin/sh\n"
        f"case \"$*\" in *{rel_source}*) exit 1;; esac\n"
        f"exec {shlex.quote(str(real_clang))} \"$@\"\n",
        encoding="utf-8",
    )
    clang.chmod(0o755)


if __name__ == "__main__":
    unittest.main()

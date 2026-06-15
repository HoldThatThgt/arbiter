# Migrated from cipher-2 tests/test_code_extractor_parallel.py (M4 facts absorption acceptance).
# Rewrites per docs/proposals/m4-test-migration-map.md:
#   * cipher2.initializer.extractor.code -> arbiter_engine.facts.extractor.code.
#   * cipher2.config.load_config(..., overrides={"extractor": {"worker_count": N}}) ->
#     c2.initializer_support.build_config(..., extractor_worker_count=N) (6-field shim; no config file).
#   * cipher2.tools.log.open_log -> the extractor's real jsonl log (arbiter_engine.facts.extractor.code).
#   * .cipher/run -> .arbiter/facts/run (map §1.6).
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arbiter_engine.facts.extractor import code as code_extractor
from arbiter_engine.facts.extractor.code import CodeFactExtractor, open_log
from c2.initializer_support import build_config
from c2.toolchain_helpers import write_fake_toolchain


def _config(target: Path, *, worker_count: int) -> code_extractor.CipherConfig:
    return build_config(
        target,
        clang_executable="bin/clang",
        gcc_executable="bin/gcc",
        extractor_worker_count=worker_count,
    )


class CodeExtractorParallelTest(unittest.TestCase):
    def test_parallel_workers_merge_out_of_order_results_by_source_path_and_resolve_calls(self):
        self._skip_if_process_pool_unavailable()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_parallel_fixture(target)
            serial = CodeFactExtractor(
                target,
                _config(target, worker_count=1),
                log_enabled=True,
            ).collect(["src"], "debug")

            parallel = CodeFactExtractor(
                target,
                _config(target, worker_count=2),
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
        self._skip_if_process_pool_unavailable()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_parallel_fixture(target)
            _wrap_clang_to_fail_source(target, "src/a.c")

            result = CodeFactExtractor(
                target,
                _config(target, worker_count=2),
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

    def test_parallel_workers_fall_back_to_serial_when_process_pool_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_parallel_fixture(target)

            with mock.patch.object(code_extractor, "ProcessPoolExecutor", _unavailable_process_pool):
                result = CodeFactExtractor(
                    target,
                    _config(target, worker_count=2),
                    log_enabled=True,
                ).collect(["src"], "debug")
            events = open_log(target).read_events(channel="initializer").events

        self.assertEqual(sorted(fact.object_name for fact in result.facts if fact.fact_kind == "function"), ["entry", "helper"])
        worker_event = next(event for event in events if event.event_name == "extractor.code.worker_pool")
        self.assertEqual(worker_event.status, "ok")
        self.assertEqual(worker_event.counts["worker_count"], 1)
        self.assertEqual(worker_event.payload["mode"], "serial")

    def test_parallel_workers_merge_header_globals_by_symbol_source_and_linkage(self):
        self._skip_if_process_pool_unavailable()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            header = target / "include" / "hooks.h"
            source_a = target / "src" / "a.c"
            source_b = target / "src" / "b.c"
            _write_source(header, "extern int (*get_attavgwidth_hook)(void);\n")
            _write_source(source_a, '#include "../include/hooks.h"\n')
            _write_source(source_b, '#include "../include/hooks.h"\n')
            write_fake_toolchain(target)
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
                _config(target, worker_count=2),
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
            stale_run = target / ".arbiter" / "facts" / "run" / "initializer-mapreduce" / "stale-run"
            stale_run.mkdir(parents=True)
            (stale_run / "segment.jsonl").write_text("{}\n", encoding="utf-8")
            os.utime(stale_run, (1, 1))
            incremental = target / ".arbiter" / "facts" / "run" / "incremental" / "stale-run"
            incremental.mkdir(parents=True)
            (incremental / "overlay.json").write_text("{}\n", encoding="utf-8")

            CodeFactExtractor(
                target,
                _config(target, worker_count=1),
                log_enabled=True,
            ).collect(["src"], "debug")
            events = open_log(target).read_events(channel="initializer").events

            worker_event = [event for event in events if event.event_name == "extractor.code.worker_pool"][-1]
            self.assertEqual(worker_event.counts["stale_run_gc_count"], 1)
            self.assertFalse(stale_run.exists())
            self.assertTrue(incremental.exists())

    def _skip_if_process_pool_unavailable(self):
        try:
            executor = code_extractor.ProcessPoolExecutor(max_workers=1)
        except OSError as exc:
            self.skipTest(f"ProcessPoolExecutor unavailable: {exc}")
        else:
            executor.shutdown(wait=True)


def _result_signature(result):
    return {
        "facts": sorted(json.dumps(fact.to_fact_record().to_json(), sort_keys=True) for fact in result.facts),
        "relatives": sorted(json.dumps(relative.to_json(), sort_keys=True) for relative in result.relatives),
        "source_inventory": sorted(json.dumps(entry.to_json(), sort_keys=True) for entry in result.source_inventory),
        "errors": [(error.code, error.source) for error in result.errors],
    }


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


def _unavailable_process_pool(*args, **kwargs):
    raise OSError("process pool unavailable for test")


if __name__ == "__main__":
    unittest.main()

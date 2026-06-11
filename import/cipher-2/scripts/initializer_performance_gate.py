from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, List

from cipher2.config import write_default_config
from cipher2.initializer import estimate_initializer_peak_bytes, initialize_repository
from cipher2.initializer.extractor.code import STREAMING_SPOOL_COMMIT_INTERVAL


WORKLOADS = [
    {"name": "small", "loc": 1_000, "files": 10, "memory_mb": 64, "timeout_seconds": 5},
    {"name": "medium", "loc": 100_000, "files": 1_000, "memory_mb": 512, "timeout_seconds": 120},
    {"name": "large", "loc": 1_000_000, "files": 10_000, "memory_mb": 2_048, "timeout_seconds": 900},
    {
        "name": "field_heavy",
        "loc": 90_000,
        "files": 300,
        "fields_per_file": 256,
        "accesses_per_file": 256,
        "memory_mb": 128,
        "timeout_seconds": 240,
        "fixture": "field_heavy",
    },
]
AVERAGE_FACT_BYTES = 512
SAFETY_MARGIN_BYTES = 32 * 1024 * 1024


def main() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            if workload.get("fixture") == "field_heavy":
                max_file_bytes = _write_field_heavy_fixture(
                    target,
                    int(workload["files"]),
                    int(workload["fields_per_file"]),
                    int(workload["accesses_per_file"]),
                )
            else:
                max_file_bytes = _write_fixture(target, int(workload["loc"]), int(workload["files"]))
            _write_fake_toolchain(target)

            tracemalloc.start()
            started = time.perf_counter()
            try:
                summary = initialize_repository(target, log_enabled=False)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            elapsed = time.perf_counter() - started

            if not summary.ok:
                raise AssertionError(f"{workload['name']} initializer failed")
            if summary.source_count != workload["files"]:
                raise AssertionError(f"{workload['name']} source count mismatch")
            peak_mb = peak / 1024 / 1024
            if peak_mb >= workload["memory_mb"]:
                raise AssertionError(f"{workload['name']} peak memory {peak_mb:.2f}MB exceeds budget")
            if elapsed >= workload["timeout_seconds"]:
                raise AssertionError(f"{workload['name']} elapsed {elapsed:.2f}s exceeds timeout")
            estimate = estimate_initializer_peak_bytes(
                max_file_bytes=max_file_bytes,
                fact_count=summary.fact_count,
                relative_count=summary.relative_count,
                function_fact_count=summary.facts_by_kind.get("function", 0),
                staging_window_count=STREAMING_SPOOL_COMMIT_INTERVAL,
                average_fact_bytes=AVERAGE_FACT_BYTES,
                streaming_write=True,
                safety_margin_bytes=SAFETY_MARGIN_BYTES,
            )
            non_streaming_estimate = estimate_initializer_peak_bytes(
                max_file_bytes=max_file_bytes,
                fact_count=summary.fact_count,
                relative_count=summary.relative_count,
                average_fact_bytes=AVERAGE_FACT_BYTES,
                streaming_write=False,
                safety_margin_bytes=SAFETY_MARGIN_BYTES,
            )
            if workload.get("fixture") == "field_heavy" and non_streaming_estimate / 1024 / 1024 <= workload["memory_mb"]:
                raise AssertionError(f"{workload['name']} fixture is too small to distinguish full materialization")
            results.append(
                {
                    "workload": workload["name"],
                    "loc": workload["loc"],
                    "files": workload["files"],
                    "facts": summary.fact_count,
                    "function_facts": summary.facts_by_kind.get("function", 0),
                    "relatives": summary.relative_count,
                    "peak_mb": round(peak_mb, 3),
                    "elapsed_seconds": round(elapsed, 3),
                    "estimated_peak_mb": round(estimate / 1024 / 1024, 3),
                    "non_streaming_estimated_peak_mb": round(non_streaming_estimate / 1024 / 1024, 3),
                }
            )

    print(json.dumps({"initializer_performance_gate": results}, ensure_ascii=False, sort_keys=True))


def _write_fixture(target: Path, loc: int, files: int) -> int:
    source_dir = target / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    loc_per_file = max(1, loc // files)
    max_file_bytes = 0
    for index in range(files):
        return_expr = f"global_{index}" if index == 0 else "func_0()"
        lines = [
            f"#define LIMIT_{index} {index}\n",
            f"int global_{index} = {index};\n",
            f"int func_{index}(void) {{ return {return_expr}; }}\n",
        ]
        remaining = max(0, loc_per_file - len(lines))
        for line in range(remaining):
            lines.append(f"/* filler {index}:{line} */\n")
        path = source_dir / f"unit_{index:05d}.c"
        path.write_text("".join(lines), encoding="utf-8")
        max_file_bytes = max(max_file_bytes, path.stat().st_size)
    return max_file_bytes


def _write_field_heavy_fixture(target: Path, files: int, fields_per_file: int, accesses_per_file: int) -> int:
    source_dir = target / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    max_file_bytes = 0
    for index in range(files):
        record = f"Heavy{index}"
        lines = [f"/* CIPHER2_FIELD_HEAVY fields={fields_per_file} accesses={accesses_per_file} */\n"]
        lines.append(f"struct {record} {{\n")
        for field_index in range(fields_per_file):
            lines.append(f"  int field_{field_index};\n")
        lines.append("};\n")
        lines.append(f"int func_{index}(struct {record} *ctx) {{\n")
        target_call = "func_1" if index == 0 and files > 1 else "func_0"
        for access_index in range(accesses_per_file):
            field_name = f"field_{access_index % fields_per_file}"
            lines.append(f"  ctx->{field_name} = {target_call}(ctx);\n")
        lines.append("  return ctx->field_0;\n")
        lines.append("}\n")
        path = source_dir / f"unit_{index:05d}.c"
        path.write_text("".join(lines), encoding="utf-8")
        max_file_bytes = max(max_file_bytes, path.stat().st_size)
    return max_file_bytes


def _write_fake_toolchain(target: Path) -> None:
    clang = target / "bin" / "clang"
    gcc = target / "bin" / "gcc"
    clang.parent.mkdir(parents=True, exist_ok=True)
    clang.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'clang version 16.0.6'; exit 0; fi\n"
        "python3 - \"$@\" <<'PY'\n"
        "import json, pathlib, re, sys\n"
        "source = next((pathlib.Path(arg) for arg in sys.argv[1:] if arg.endswith(('.c','.h'))), pathlib.Path('unit.c'))\n"
        "text = source.read_text(encoding='utf-8') if source.exists() else ''\n"
        "def loc(line): return {'line': line, 'file': str(source)}\n"
        "def qtype(text): return {'qualType': text}\n"
        "if 'cipher2_toolchain_probe' in text:\n"
        "    field_id = 'field:cipher2_probe_record:member'\n"
        "    print(json.dumps({'kind':'TranslationUnitDecl','inner':[{'kind':'RecordDecl','name':'cipher2_probe_record','loc':loc(1),'type':qtype('struct cipher2_probe_record'),'completeDefinition':True,'inner':[{'id':field_id,'kind':'FieldDecl','name':'member','loc':loc(1),'type':qtype('int'),'ownerName':'cipher2_probe_record'}]},{'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]},{'kind':'FunctionDecl','name':'cipher2_toolchain_probe','loc':loc(3),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[{'kind':'CallExpr','loc':loc(4),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':'cipher2_probe_callee','loc':loc(4),'type':qtype('int (int)'),'referencedDecl':{'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)')}},{'kind':'MemberExpr','name':'member','loc':loc(4),'type':qtype('int'),'referencedMemberDecl':field_id}]}]}]}]}))\n"
        "    raise SystemExit(0)\n"
        "marker = re.search(r'CIPHER2_FIELD_HEAVY fields=(\\d+) accesses=(\\d+)', text)\n"
        "if marker:\n"
        "    field_count = int(marker.group(1))\n"
        "    access_count = int(marker.group(2))\n"
        "    index_match = re.search(r'unit_(\\d+)', source.name)\n"
        "    index = int(index_match.group(1)) if index_match else 0\n"
        "    record_name = f'Heavy{index}'\n"
        "    function_name = f'func_{index}'\n"
        "    target_name = 'func_1' if index == 0 else 'func_0'\n"
        "    fields = []\n"
        "    for field_index in range(field_count):\n"
        "        field_id = f'field:{record_name}:field_{field_index}'\n"
        "        fields.append({'id':field_id,'kind':'FieldDecl','name':f'field_{field_index}','loc':loc(2 + field_index),'type':qtype('int'),'ownerName':record_name})\n"
        "    body = []\n"
        "    for access_index in range(access_count):\n"
        "        field_index = access_index % field_count\n"
        "        field_name = f'field_{field_index}'\n"
        "        field_id = f'field:{record_name}:{field_name}'\n"
        "        line = 3 + field_count + access_index\n"
        "        body.append({'kind':'BinaryOperator','opcode':'=','loc':loc(line),'type':qtype('int'),'inner':[{'kind':'MemberExpr','name':field_name,'loc':loc(line),'type':qtype('int'),'referencedMemberDecl':field_id,'isArrow':True},{'kind':'CallExpr','loc':loc(line),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':target_name,'loc':loc(line),'type':qtype('int (void)'),'referencedDecl':{'kind':'FunctionDecl','name':target_name,'loc':loc(line),'type':qtype('int (void)')}}]}]})\n"
        "    print(json.dumps({'kind':'TranslationUnitDecl','inner':[{'kind':'RecordDecl','name':record_name,'loc':loc(1),'type':qtype('struct ' + record_name),'completeDefinition':True,'inner':fields},{'kind':'FunctionDecl','name':function_name,'loc':loc(3 + field_count),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':body}]}]}))\n"
        "    raise SystemExit(0)\n"
        "match = re.search(r'\\b([A-Za-z_]\\w*)\\s*\\([^;]*\\)\\s*\\{', text)\n"
        "name = match.group(1) if match else source.stem\n"
        "call_match = re.search(r'return\\s+([A-Za-z_]\\w*)\\s*\\(', text)\n"
        "call_name = call_match.group(1) if call_match else None\n"
        "body = []\n"
        "if call_name and call_name != name:\n"
        "    body.append({'kind':'CallExpr','loc':loc(2),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':call_name,'loc':loc(2),'type':qtype('int (void)'),'referencedDecl':{'kind':'FunctionDecl','name':call_name,'loc':loc(2),'type':qtype('int (void)')}}]})\n"
        "print(json.dumps({'kind':'TranslationUnitDecl','inner':[{'kind':'FunctionDecl','name':name,'loc':loc(1),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':body}]}]}))\n"
        "PY\n",
        encoding="utf-8",
    )
    gcc.write_text("#!/bin/sh\necho 'gcc (GCC) 10.5.0'\n", encoding="utf-8")
    clang.chmod(0o755)
    gcc.chmod(0o755)
    write_default_config(target, clang_executable="bin/clang", gcc_executable="bin/gcc", observe=False)


if __name__ == "__main__":
    main()

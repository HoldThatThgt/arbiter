from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, List

from cipher2.config import load_config, write_default_config
from cipher2.initializer.extractor.code import CodeFactExtractor


WORKLOADS = [
    {"name": "small", "loc": 1_000, "files": 10, "memory_mb": 64, "timeout_seconds": 5},
    {"name": "medium", "loc": 100_000, "files": 1_000, "memory_mb": 512, "timeout_seconds": 120},
    {"name": "large", "loc": 1_000_000, "files": 10_000, "memory_mb": 2_048, "timeout_seconds": 900},
]


def main() -> None:
    results: List[Dict[str, float]] = []
    for workload in WORKLOADS:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_fixture(target, int(workload["loc"]), int(workload["files"]))
            _write_fake_toolchain(target)
            extractor = CodeFactExtractor(target, load_config(target, observe=False), log_enabled=False)

            tracemalloc.start()
            started = time.perf_counter()
            try:
                result = extractor.collect(["src"], "default")
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            elapsed = time.perf_counter() - started
            if result.source_count != workload["files"]:
                raise AssertionError(f"{workload['name']} source count mismatch")
            if result.errors:
                raise AssertionError(f"{workload['name']} extractor returned errors")
            peak_mb = peak / 1024 / 1024
            if peak_mb >= workload["memory_mb"]:
                raise AssertionError(f"{workload['name']} peak memory {peak_mb:.2f}MB exceeds budget")
            if elapsed >= workload["timeout_seconds"]:
                raise AssertionError(f"{workload['name']} elapsed {elapsed:.2f}s exceeds timeout")
            results.append(
                {
                    "workload": workload["name"],
                    "loc": workload["loc"],
                    "files": workload["files"],
                    "facts": len(result.facts),
                    "relatives": len(result.relatives),
                    "peak_mb": round(peak_mb, 3),
                    "elapsed_seconds": round(elapsed, 3),
                }
            )

    print(json.dumps({"clang_extractor_performance_gate": results}, ensure_ascii=False, sort_keys=True))


def _write_fixture(target: Path, loc: int, files: int) -> None:
    source_dir = target / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    loc_per_file = max(1, loc // files)
    for index in range(files):
        return_expr = str(index) if index == 0 else "func_0()"
        lines = [f"#define LIMIT_{index} {index}\n"]
        if index != 0:
            lines.append("int func_0(void);\n")
        lines.append(f"int func_{index}(void) {{ return {return_expr}; }}\n")
        for line in range(max(0, loc_per_file - len(lines))):
            lines.append(f"/* filler {index}:{line} */\n")
        (source_dir / f"unit_{index:05d}.c").write_text("".join(lines), encoding="utf-8")


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

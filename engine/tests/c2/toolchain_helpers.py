# Ported from cipher-2 tests/toolchain_helpers.py (M4 test support): the fake clang/gcc + JSON-AST
# oracle is verbatim; the cipher2 write_default_config tail is replaced by returning an ExtractorConfig.
# (The incremental tests' load_config shim lives in c2.incremental_support, not here.)
from pathlib import Path

from arbiter_engine.facts.extractor.code._shim import ExtractorConfig


def write_fake_toolchain(
    target: Path,
    *,
    clang_version: str = "16.0.6",
    gcc_version: str = "10.5.0",
    compile_database_path=None,
    extractor_worker_count: int = 1,
) -> ExtractorConfig:
    clang = target / "bin" / "clang"
    gcc = target / "bin" / "gcc"
    clang.parent.mkdir(parents=True, exist_ok=True)
    clang.write_text(_fake_clang_script(clang_version), encoding="utf-8")
    gcc.write_text(f"#!/bin/sh\necho 'gcc (GCC) {gcc_version}'\n", encoding="utf-8")
    clang.chmod(0o755)
    gcc.chmod(0o755)
    config = ExtractorConfig(
        clang_executable=str(clang),
        gcc_executable=str(gcc),
        compile_database_path=compile_database_path,
        extractor_worker_count=extractor_worker_count,
    )
    # Mirror cipher-2's write_fake_toolchain, which persisted a config that a later
    # load_config(target) read back. The extractor-config registry lives in
    # initializer_support; stash there so load_config(target) returns this toolchain.
    from c2.initializer_support import _CONFIG_BY_TARGET

    _CONFIG_BY_TARGET[str(Path(target).resolve(strict=False))] = config
    return config


def _fake_clang_script(version: str) -> str:
    return (
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        f"  echo 'clang version {version}'\n"
        "  exit 0\n"
        "fi\n"
        "python3 - \"$@\" <<'PY'\n"
        "import json, re, sys\n"
        "CONTROL = {'if','for','while','switch','return','sizeof'}\n"
        "source = None\n"
        "for arg in sys.argv[1:]:\n"
        "    if arg.endswith(('.c','.h','.cc','.cpp','.cxx','.hh','.hpp','.hxx')):\n"
        "        source = arg\n"
        "if source is None:\n"
        "    print('{}')\n"
        "    raise SystemExit(0)\n"
        "lines = open(source, encoding='utf-8').read().splitlines()\n"
        "nodes = []\n"
        "current = None\n"
        "depth = 0\n"
        "assigned = set()\n"
        "fn_re = re.compile(r'^\\s*(?:[A-Za-z_][\\w\\s\\*]*\\s+)+([A-Za-z_]\\w*)\\s*\\([^;]*\\)\\s*\\{')\n"
        "call_re = re.compile(r'\\b([A-Za-z_]\\w*)\\s*\\(')\n"
        "assign_re = re.compile(r'\\b([A-Za-z_]\\w*)\\s*=\\s*([A-Za-z_]\\w*)')\n"
        "def loc(line): return {'line': line, 'file': source}\n"
        "def qtype(text): return {'qualType': text}\n"
        "if any('cipher2_toolchain_probe' in line for line in lines):\n"
        "    field_id = 'field:cipher2_probe_record:member'\n"
        "    field = {'id':field_id,'kind':'FieldDecl','name':'member','loc':loc(1),'type':qtype('int'),'ownerName':'cipher2_probe_record'}\n"
        "    print(json.dumps({'kind':'TranslationUnitDecl','inner':[\n"
        "        {'kind':'RecordDecl','name':'cipher2_probe_record','loc':loc(1),'completeDefinition':True,'type':qtype('struct cipher2_probe_record'),'inner':[field]},\n"
        "        {'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]},\n"
        "        {'kind':'FunctionDecl','name':'cipher2_toolchain_probe','loc':loc(3),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[{'kind':'CallExpr','loc':loc(4),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':'cipher2_probe_callee','loc':loc(4),'type':qtype('int (int)'),'referencedDecl':{'kind':'FunctionDecl','name':'cipher2_probe_callee','loc':loc(2),'type':qtype('int (int)')}},{'kind':'MemberExpr','name':'member','loc':loc(4),'type':qtype('int'),'referencedMemberDecl':field_id}]}]}]}\n"
        "    ]}, sort_keys=True))\n"
        "    raise SystemExit(0)\n"
        "def body_nodes(text, line_no, fn_name):\n"
        "    out = []\n"
        "    for lhs, rhs in assign_re.findall(text):\n"
        "        assigned.add(lhs)\n"
        "        out.append({'kind':'BinaryOperator','opcode':'=','loc':loc(line_no),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':lhs,'loc':loc(line_no),'type':qtype('int (*)(void)')},{'kind':'DeclRefExpr','name':rhs,'loc':loc(line_no),'type':qtype('int'),'referencedDecl':{'kind':'FunctionDecl','name':rhs,'loc':loc(line_no),'type':qtype('int (void)')}}]})\n"
        "    for match in call_re.finditer(text):\n"
        "        name = match.group(1)\n"
        "        if name in CONTROL or name == fn_name:\n"
        "            continue\n"
        "        out.append({'kind':'CallExpr','loc':loc(line_no),'type':qtype('int'),'inner':[{'kind':'DeclRefExpr','name':name,'loc':loc(line_no),'type':qtype('int (void)'),'referencedDecl':{'kind':'FunctionDecl','name':name,'loc':loc(line_no),'type':qtype('int (void)')}}]})\n"
        "    return out\n"
        "for line_no, raw in enumerate(lines, 1):\n"
        "    text = raw.strip()\n"
        "    if not text:\n"
        "        continue\n"
        "    if current is None:\n"
        "        macro = re.match(r'^#\\s*define\\s+([A-Za-z_]\\w*)', text)\n"
        "        if macro:\n"
            "            nodes.append({'kind':'MacroDefinitionRecord','name':macro.group(1),'loc':loc(line_no)})\n"
        "            continue\n"
        "        include = re.match(r'^#\\s*include\\s+[<\\\"]([^>\\\"]+)[>\\\"]', text)\n"
        "        if include:\n"
            "            nodes.append({'kind':'InclusionDirective','name':include.group(1),'loc':loc(line_no)})\n"
        "            continue\n"
        "        struct = re.search(r'\\b(struct|union|enum)\\s+([A-Za-z_]\\w*)', text)\n"
        "        if struct:\n"
            "            node = {'kind':'RecordDecl' if struct.group(1) != 'enum' else 'EnumDecl','name':struct.group(2),'loc':loc(line_no),'type':qtype(struct.group(1) + ' ' + struct.group(2)),'completeDefinition':True,'inner':[]}\n"
        "            field = re.search(r'\\{\\s*\\w+\\s+([A-Za-z_]\\w*)\\s*;', text)\n"
        "            if field:\n"
            "                field_id = 'field:' + struct.group(2) + ':' + field.group(1)\n"
            "                node['inner'].append({'id':field_id,'kind':'FieldDecl','name':field.group(1),'loc':loc(line_no),'type':qtype('int'),'ownerName':struct.group(2)})\n"
        "            nodes.append(node)\n"
        "            continue\n"
        "        typedef = re.search(r'\\btypedef\\b.*?\\b([A-Za-z_]\\w*)\\s*;', text)\n"
        "        if typedef:\n"
            "            nodes.append({'kind':'TypedefDecl','name':typedef.group(1),'loc':loc(line_no),'type':qtype(typedef.group(1))})\n"
        "            continue\n"
        "        fn = fn_re.match(text)\n"
        "        if fn:\n"
        "            name = fn.group(1)\n"
            "            current = {'kind':'FunctionDecl','name':name,'loc':loc(line_no),'type':qtype('int (void)'),'isThisDeclarationADefinition':True,'inner':[{'kind':'CompoundStmt','inner':[]}]}\n"
        "            assigned = set()\n"
        "            body = text.split('{',1)[1]\n"
        "            current['inner'][0]['inner'].extend(body_nodes(body, line_no, name))\n"
        "            depth = raw.count('{') - raw.count('}')\n"
        "            if depth <= 0:\n"
        "                nodes.append(current)\n"
        "                current = None\n"
        "            continue\n"
        "        glob = re.match(r'^\\s*(?:static\\s+|extern\\s+|const\\s+|volatile\\s+|unsigned\\s+|signed\\s+|long\\s+|short\\s+)*[A-Za-z_]\\w*(?:\\s*\\*)?\\s+([A-Za-z_]\\w*)\\s*(?:=[^;]*)?;', text)\n"
        "        if glob and '(' not in text.split(';',1)[0]:\n"
            "            nodes.append({'kind':'VarDecl','name':glob.group(1),'loc':loc(line_no),'type':qtype('int')})\n"
        "            continue\n"
        "    else:\n"
        "        current['inner'][0]['inner'].extend(body_nodes(text, line_no, current['name']))\n"
        "        depth += raw.count('{') - raw.count('}')\n"
        "        if depth <= 0:\n"
        "            nodes.append(current)\n"
        "            current = None\n"
        "print(json.dumps({'kind':'TranslationUnitDecl','inner':nodes}, sort_keys=True))\n"
        "PY\n"
    )

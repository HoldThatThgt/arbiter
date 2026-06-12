"""Regenerate the facts-conformance corpus from the engine.

History (ADR-0013): the corpus lines were originally RECORDED FROM cipher-2
(the in-tree reference at import/cipher-2, commit "import: cipher-2 @main");
before the reference tree was retired, this generator was flipped to record
from the engine and verified to reproduce the cipher-recorded corpus
byte-for-byte. The corpus is therefore the permanent byte-pin of the frozen
``search``/``detail`` surface:

- EXISTING lines are immutable. test_facts_conformance_corpus.py replays them
  against the engine and asserts byte-equality; a regeneration that changes
  an existing line is an adjudication-surface regression, not a refresh.
- NEW scenarios may be appended when the engine grows real behavior (e.g.
  populated snapshots once the cipher-2 query engine is absorbed). Per
  ADR-0013 such extensions must be cross-checked against upstream cipher-2
  out-of-tree (the frozen source repo, or this repo's import commit) before
  the recorded lines become the pin.

Usage:

    PYTHONPATH=engine python3 engine/tests/write_facts_conformance_corpus.py

Output: engine/tests/fixtures/facts_conformance/empty_corpus.jsonl — one line
per scenario, each the canonical JSON (sorted keys, compact separators) of

    {"arguments": ..., "masked": [...], "name": ..., "result": ...}

where ``result`` is the engine's tools/call ``result`` payload and ``masked``
lists the volatile dotted paths replaced with "<volatile>" (the same masking
write_transcripts.py applies: ``structuredContent.overlay_id``).

The generator is deterministic: running it twice must produce identical
bytes (CI-checked indirectly by the replay test's byte-equality assertion).
"""

import io
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]

import sys  # noqa: E402

ENGINE_SRC = REPO / "engine"
if str(ENGINE_SRC) not in sys.path:
    sys.path.insert(0, str(ENGINE_SRC))

from arbiter_engine import rpc  # noqa: E402

CORPUS_PATH = REPO / "engine" / "tests" / "fixtures" / "facts_conformance" / "empty_corpus.jsonl"

# The original scenario set recorded from cipher-2: search over the empty
# corpus and detail of a missing fact across every budget tier.
CASES = [
    ("search", {"query": "alpha", "limit": 1}),
    ("search", {"query": "", "limit": 2}),
    ("detail", {"fact_id": "missing", "budget": "small"}),
    ("detail", {"fact_id": "missing", "budget": "normal"}),
    ("detail", {"fact_id": "missing", "budget": "large"}),
]

# Volatile dotted paths inside the tool result payload, masked the way
# write_transcripts.py masks them (relative to ``result`` there, relative to
# the recorded ``result`` object here).
VOLATILE_PATHS = ["structuredContent.overlay_id"]


@contextmanager
def working_dir(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def mask_path(value, path, replacement):
    """Replace the dotted ``path`` inside ``value`` when present.

    Returns True when the leaf existed and was replaced (mirrors
    write_transcripts.py _set_path semantics for dict payloads).
    """
    parts = path.split(".")
    cursor = value
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return False
        cursor = cursor[part]
    last = parts[-1]
    if not isinstance(cursor, dict) or last not in cursor:
        return False
    cursor[last] = replacement
    return True


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def tool_call(name, arguments):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        separators=(",", ":"),
    ) + "\n"


def engine_result(name, arguments):
    stdin = io.StringIO(tool_call(name, arguments))
    stdout = io.StringIO()
    rpc.serve(stdin, stdout)
    return json.loads(stdout.getvalue())["result"]


def record_cases():
    entries = []
    with tempfile.TemporaryDirectory() as tmp, working_dir(tmp):
        with mock.patch.dict(os.environ, {"ARBITER_ENGINE_ROLE": "QUERY", "ARBITER_ENGINE_SEAT": "executor"}):
            for name, arguments in CASES:
                result = engine_result(name, arguments)
                masked = [path for path in VOLATILE_PATHS if mask_path(result, path, "<volatile>")]
                entries.append(
                    {
                        "name": name,
                        "arguments": arguments,
                        "masked": masked,
                        "result": result,
                    }
                )
    return entries


def main():
    entries = record_cases()
    CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CORPUS_PATH.write_text(
        "\n".join(canonical(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

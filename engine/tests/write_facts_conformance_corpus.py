"""Generate the recorded facts-conformance corpus (issue #42).

The live conformance test (test_facts_conformance.py) compares the engine
against the in-tree cipher-2 reference server. This generator freezes that
reference surface into a standalone recorded corpus so the byte-freeze of the
``search``/``detail`` tool responses survives even if import/cipher-2 changes
or is removed from the tree.

Regenerate (only when an owner-signed ADR changes the frozen surface) with:

    PYTHONPATH=engine python3 engine/tests/write_facts_conformance_corpus.py

Output: engine/tests/fixtures/facts_conformance/empty_corpus.jsonl — one line
per scenario, each the canonical JSON (sorted keys, compact separators) of

    {"arguments": ..., "masked": [...], "name": ..., "result": ...}

where ``result`` is the cipher-2 ``call_tool(...).to_json()`` payload recorded
over an empty corpus, and ``masked`` lists the volatile dotted paths replaced
with "<volatile>" (the same masking write_transcripts.py applies:
``structuredContent.overlay_id``). The scenario set mirrors the live test:
search and detail across every budget tier plus the empty-corpus search
cases. test_facts_conformance_corpus.py replays each recorded request against
the engine (never cipher-2) and asserts byte-equality.

The generator is deterministic: running it twice must produce identical
bytes (CI-checked indirectly by the replay test's byte-equality assertion).
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CIPHER_SRC = REPO / "import" / "cipher-2" / "src"
if str(CIPHER_SRC) not in sys.path:
    sys.path.insert(0, str(CIPHER_SRC))

from cipher2.mcp import open_mcp_server  # noqa: E402

CORPUS_PATH = REPO / "engine" / "tests" / "fixtures" / "facts_conformance" / "empty_corpus.jsonl"

# Same scenario set the live test exercises: search over the empty corpus and
# detail of a missing fact across every budget tier.
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


def record_cases():
    entries = []
    with tempfile.TemporaryDirectory() as tmp, working_dir(tmp):
        server = open_mcp_server(Path(tmp))
        for name, arguments in CASES:
            result = server.call_tool(name, arguments).to_json()
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

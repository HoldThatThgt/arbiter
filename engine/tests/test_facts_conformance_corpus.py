"""Replay the recorded facts-conformance corpus against the engine.

Unlike test_facts_conformance.py (which compares the engine live against the
in-tree cipher-2 reference server), this test replays the recorded corpus
written by write_facts_conformance_corpus.py and asserts byte-equality, so
the frozen search/detail surface stays pinned even if import/cipher-2
changes or is removed. It must never import cipher-2.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from arbiter_engine import rpc

CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "facts_conformance" / "empty_corpus.jsonl"
EXPECTED_CASES = 5


@contextmanager
def working_dir(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def response_for(line):
    stdin = io.StringIO(line)
    stdout = io.StringIO()
    rpc.serve(stdin, stdout)
    return json.loads(stdout.getvalue())


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


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def mask_path(value, path, replacement):
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


class FactsConformanceCorpusTest(unittest.TestCase):
    def test_engine_matches_recorded_corpus_bytes(self):
        lines = CORPUS_PATH.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), EXPECTED_CASES, "corpus scenario count drifted; regenerate deliberately")

        with tempfile.TemporaryDirectory() as tmp, working_dir(tmp):
            with mock.patch.dict(os.environ, {"ARBITER_ENGINE_ROLE": "QUERY", "ARBITER_ENGINE_SEAT": "executor"}):
                for line in lines:
                    case = json.loads(line)
                    with self.subTest(name=case["name"], arguments=case["arguments"]):
                        actual = response_for(tool_call(case["name"], case["arguments"]))["result"]
                        # Mask exactly the volatile paths the recording masked;
                        # each must be present live too, or the surface drifted.
                        masked = [
                            path
                            for path in case["masked"]
                            if mask_path(actual, path, "<volatile>")
                        ]
                        self.assertEqual(masked, case["masked"])

                        self.assertEqual(canonical(actual), canonical(case["result"]))


if __name__ == "__main__":
    unittest.main()

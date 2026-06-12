import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from arbiter_engine import rpc

REPO = Path(__file__).resolve().parents[2]
CIPHER_SRC = REPO / "import" / "cipher-2" / "src"
if str(CIPHER_SRC) not in sys.path:
    sys.path.insert(0, str(CIPHER_SRC))

from cipher2.mcp import open_mcp_server  # noqa: E402


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


class FactsConformanceTest(unittest.TestCase):
    def test_search_and_detail_match_cipher2_empty_corpus_bytes(self):
        cases = [
            ("search", {"query": "alpha", "limit": 1}),
            ("search", {"query": "", "limit": 2}),
            ("detail", {"fact_id": "missing", "budget": "small"}),
            ("detail", {"fact_id": "missing", "budget": "normal"}),
            ("detail", {"fact_id": "missing", "budget": "large"}),
        ]

        with tempfile.TemporaryDirectory() as tmp, working_dir(tmp):
            expected_server = open_mcp_server(Path(tmp))
            with mock.patch.dict(os.environ, {"ARBITER_ENGINE_ROLE": "QUERY", "ARBITER_ENGINE_SEAT": "executor"}):
                for name, arguments in cases:
                    with self.subTest(name=name, arguments=arguments):
                        expected = expected_server.call_tool(name, arguments).to_json()
                        actual = response_for(tool_call(name, arguments))["result"]

                        self.assertEqual(canonical(actual), canonical(expected))


if __name__ == "__main__":
    unittest.main()

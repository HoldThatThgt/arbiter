import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List


def _tool_call(request_id: int, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


REQUESTS: List[Dict[str, Any]] = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"client": "transcript-replay"},
    },
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    _tool_call(3, "detail", {"id": "fact:1"}),
    _tool_call(4, "import_recipes", {"path": "recipes.yml"}),
    _tool_call(5, "recipe_search", {"query": "gtest"}),
    _tool_call(6, "register", {"path": "recipes.yml"}),
    _tool_call(7, "run", {"recipe": "unit"}),
    _tool_call(8, "scan", {"scope": "tests"}),
    _tool_call(9, "search", {"query": "callers:main"}),
    _tool_call(10, "search", {"query": "callers:main", "extra": True}),
    {"jsonrpc": "2.0", "id": 11, "method": "arbiter/nope"},
    {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "arbiter/handshake",
        "params": {"expected_version": "dev"},
    },
    {
        "jsonrpc": "2.0",
        "id": 13,
        "method": "arbiter/handshake",
        "params": {"expected_version": "old"},
    },
    {
        "jsonrpc": "2.0",
        "id": 14,
        "method": "arbiter/startRun",
        "params": {"duration_ms": 0, "timeout_ms": 1000},
    },
    {
        "jsonrpc": "2.0",
        "id": 15,
        "method": "arbiter/runStatus",
        "params": {"run_id": "$last_run_id"},
    },
]


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    transcript = repo / "testdata" / "transcripts" / "hello_world.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    entries = record(repo)
    transcript.write_text(
        "\n".join(json.dumps(entry, separators=(",", ":")) for entry in entries) + "\n",
        encoding="utf-8",
    )
    return 0


def record(repo: Path) -> List[Dict[str, Any]]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "engine")
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.Popen(
            [sys.executable, "-m", "arbiter_engine.rpc"],
            cwd=tmp,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None

        entries: List[Dict[str, Any]] = []
        last_run_id = ""
        try:
            for template in REQUESTS:
                request = _materialize_request(template, last_run_id)
                if request["method"] == "arbiter/runStatus":
                    time.sleep(0.05)
                proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if line == "":
                    raise AssertionError("rpc stub exited before writing a response")
                response = json.loads(line)
                if request["method"] == "arbiter/startRun":
                    last_run_id = response["result"]["run_id"]
                entries.append({"type": "request", "message": request})
                entries.append(
                    {
                        "type": "response",
                        "message": response,
                        "allow_volatile": [],
                    }
                )
        finally:
            proc.stdin.close()
            stderr = proc.stderr.read() if proc.stderr else ""
            code = proc.wait(timeout=5)
            proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    if code != 0:
        raise AssertionError(f"rpc stub exited {code}: {stderr}")
    return entries


def _materialize_request(template: Dict[str, Any], last_run_id: str) -> Dict[str, Any]:
    request = json.loads(json.dumps(template))
    params = request.get("params")
    if isinstance(params, dict) and params.get("run_id") == "$last_run_id":
        params["run_id"] = last_run_id
    return request


if __name__ == "__main__":
    raise SystemExit(main())

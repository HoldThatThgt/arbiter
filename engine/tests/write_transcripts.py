import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


REQUESTS: List[Dict[str, Any]] = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"client": "transcript-replay"},
    },
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "ping", "arguments": {"message": "hello"}},
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
    proc = subprocess.Popen(
        [sys.executable, "-m", "arbiter_engine.rpc"],
        cwd=repo,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    entries: List[Dict[str, Any]] = []
    try:
        for request in REQUESTS:
            proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if line == "":
                raise AssertionError("rpc stub exited before writing a response")
            entries.append({"type": "request", "message": request})
            entries.append(
                {
                    "type": "response",
                    "message": json.loads(line),
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


if __name__ == "__main__":
    raise SystemExit(main())

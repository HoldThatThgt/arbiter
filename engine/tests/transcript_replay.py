import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List


def load_transcript(path: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def transcript_paths(repo: Path) -> Iterable[Path]:
    return sorted((repo / "testdata" / "transcripts").glob("*.jsonl"))


def replay_with_python(repo: Path, transcript: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "engine")
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        (workdir / "engine").symlink_to(repo / "engine", target_is_directory=True)
        proc = subprocess.Popen(
            [sys.executable, "-m", "arbiter_engine.rpc"],
            cwd=workdir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None

        try:
            for request, response in _pairs(load_transcript(transcript)):
                if request["message"].get("method") == "arbiter/runStatus":
                    time.sleep(0.05)
                proc.stdin.write(json.dumps(request["message"], separators=(",", ":")) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if line == "":
                    raise AssertionError("rpc stub exited before writing a response")
                actual = json.loads(line)
                expected = response["message"]
                _assert_matches(expected, actual, response.get("allow_volatile", []))
                if request["message"].get("method") == "arbiter/startRun":
                    time.sleep(0.1)
        finally:
            proc.stdin.close()
            stderr = proc.stderr.read() if proc.stderr else ""
            code = proc.wait(timeout=5)
            proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

        if code != 0:
            raise AssertionError(f"rpc stub exited {code}: {stderr}")


def _pairs(entries: List[Dict[str, Any]]) -> Iterable[tuple[Dict[str, Any], Dict[str, Any]]]:
    if len(entries) % 2:
        raise AssertionError("transcript must contain request/response pairs")
    for index in range(0, len(entries), 2):
        request = entries[index]
        response = entries[index + 1]
        if request.get("type") != "request" or response.get("type") != "response":
            raise AssertionError(f"invalid pair starting at line {index + 1}")
        yield request, response


def _assert_matches(expected: Any, actual: Any, allow_volatile: Iterable[str]) -> None:
    for path in allow_volatile:
        expected = _without_path(expected, path)
        actual = _without_path(actual, path)
    if actual != expected:
        raise AssertionError(f"response mismatch:\nexpected={expected!r}\nactual={actual!r}")


def _without_path(value: Any, path: str) -> Any:
    if not path:
        return value
    parts = path.split(".")
    clone = json.loads(json.dumps(value))
    cursor = clone
    for part in parts[:-1]:
        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
    last = parts[-1]
    if isinstance(cursor, list):
        cursor[int(last)] = None
    else:
        cursor.pop(last, None)
    return clone

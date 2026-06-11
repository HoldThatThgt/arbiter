import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple



def _tool_call(request_id: int, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _request(request_id: int, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    request: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return request


def _scenario(name: str, *requests: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    return name, list(requests)


SCENARIOS: List[Tuple[str, List[Dict[str, Any]]]] = [
    _scenario("initialize", _request(1, "initialize", {"client": "transcript-replay"})),
    _scenario("tools_list", _request(1, "tools/list")),
    _scenario("tool_detail_default", _tool_call(1, "detail", {"fact_id": "fact:1"})),
    _scenario("tool_detail_budget_small", _tool_call(1, "detail", {"fact_id": "fact:1", "budget": "small"})),
    _scenario("tool_detail_budget_normal", _tool_call(1, "detail", {"fact_id": "fact:1", "budget": "normal"})),
    _scenario("tool_detail_budget_large", _tool_call(1, "detail", {"fact_id": "fact:1", "budget": "large"})),
    _scenario("tool_import_recipes", _tool_call(1, "import_recipes", {"path": "engine/tests/fixtures/recipes/v2_basic.yaml"})),
    _scenario("tool_recipe_search", _tool_call(1, "recipe_search", {"query": "unit"})),
    _scenario("tool_register", _tool_call(1, "register", {"path": "engine/tests/fixtures/recipes/v2_basic.yaml"})),
    _scenario("tool_run", _tool_call(1, "run", {"recipe": "unit", "options": {"harness_options": {"gtest": {"fail_fast": False}}}})),
    _scenario("tool_scan", _tool_call(1, "scan", {"scope": "tests"})),
    _scenario("tool_search_default", _tool_call(1, "search", {"query": "callers:main"})),
    _scenario("tool_search_limit_min", _tool_call(1, "search", {"query": "callers:main", "limit": 1})),
    _scenario("tool_search_limit_default", _tool_call(1, "search", {"query": "callers:main", "limit": 20})),
    _scenario("tool_search_limit_max", _tool_call(1, "search", {"query": "callers:main", "limit": 50})),
    _scenario("error_invalid_args", _tool_call(1, "search", {"query": "callers:main", "extra": True})),
    _scenario("error_invalid_meta", _request(1, "tools/call", {"name": "search", "arguments": {"query": "q"}, "_meta": "bad"})),
    _scenario("error_invalid_params", _request(1, "arbiter/refresh", {"scope": "bad"})),
    _scenario("error_method_not_found", _request(1, "arbiter/nope")),
    _scenario("error_tool_not_found", _request(1, "tools/call", {"name": "missing", "arguments": {}})),
    _scenario("custom_handshake", _request(1, "arbiter/handshake", {"expected_version": "dev"})),
    _scenario("custom_handshake_stale", _request(1, "arbiter/handshake", {"expected_version": "old"})),
    _scenario("custom_refresh", _request(1, "arbiter/refresh", {"scope": {"paths": ["src"]}, "_meta": {"match_id": "m1"}})),
    _scenario(
        "custom_census",
        _request(1, "arbiter/census", {"scope": {"globs": ["transcript-no-such-dir/**/*.c"]}}),
    ),
    _scenario("custom_resolve_briefing", _request(1, "arbiter/resolveBriefing", {"refs": ["fact:1"]})),
    _scenario(
        "custom_start_run",
        _request(
            1,
            "arbiter/startRun",
            {"spec": {"kind": "stub", "sleep_ms": 0, "timeout_s": 1, "result": {"overall": "passed"}}},
        ),
    ),
    _scenario("custom_run_status_unknown", _request(1, "arbiter/runStatus", {"run_id": "transcript-missing-run"})),
]


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    directory = repo / "testdata" / "transcripts"
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.jsonl"):
        old.unlink()
    for name, requests in SCENARIOS:
        entries = record(repo, requests)
        (directory / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(entry, separators=(",", ":")) for entry in entries) + "\n",
            encoding="utf-8",
        )
    return 0


def record(repo: Path, requests: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
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

        entries: List[Dict[str, Any]] = []
        try:
            for request in requests:
                proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if line == "":
                    raise AssertionError("rpc stub exited before writing a response")
                allow_volatile = _volatile_paths(request)
                message = json.loads(line)
                present_volatile: List[str] = []
                for path in allow_volatile:
                    if _set_path(message, path, "<volatile>"):
                        present_volatile.append(path)
                entries.append({"type": "request", "message": request})
                entries.append(
                    {
                        "type": "response",
                        "message": message,
                        "allow_volatile": present_volatile,
                    }
                )
                if request.get("method") == "arbiter/startRun":
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
    return entries


def _volatile_paths(request: Dict[str, Any]) -> List[str]:
    if request.get("method") == "arbiter/startRun":
        return ["result.run_id"]
    if request.get("method") == "arbiter/refresh":
        params = request.get("params", {})
        if isinstance(params.get("scope"), dict):
            return ["result.overlay_id"]
    if request.get("method") == "tools/call":
        params = request.get("params", {})
        arguments = params.get("arguments", {})
        if params.get("name") == "search" and set(arguments) <= {"query", "limit"}:
            return ["result.structuredContent.overlay_id"]
        if params.get("name") == "detail" and set(arguments) <= {"fact_id", "budget"}:
            return ["result.structuredContent.overlay_id"]
        if params.get("name") == "run":
            return ["error.data.detail"]
    return []


def _set_path(value: Dict[str, Any], path: str, replacement: Any) -> bool:
    parts = path.split(".")
    cursor: Any = value
    for part in parts[:-1]:
        try:
            cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
        except (KeyError, IndexError):
            return False
    last = parts[-1]
    try:
        if isinstance(cursor, list):
            cursor[int(last)] = replacement
        else:
            if last not in cursor:
                return False
            cursor[last] = replacement
    except (KeyError, IndexError):
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())

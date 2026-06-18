"""Typed JSON-RPC error helpers for the Arbiter engine."""

from __future__ import annotations

from typing import Any, Mapping


SPEC_ERROR_KINDS = frozenset(
    {
        "no_snapshot",
        "briefing_unresolved",
        "capability_revoked",
        "recipe_pin_mismatch",
        "engine_stale",
        "harness_unavailable",
        "indexer_unavailable",
        "lock_timeout",
    }
)

CHASSIS_ERROR_KINDS = frozenset(
    {
        "internal_error",
        "invalid_args",
        "invalid_json",
        "invalid_jsonrpc",
        "invalid_meta",
        "invalid_method",
        "invalid_params",
        "invalid_request",
        "line_too_large",
        "method_not_found",
        "schema_invalid",
        "tool_not_found",
    }
)

KNOWN_ERROR_KINDS = SPEC_ERROR_KINDS | CHASSIS_ERROR_KINDS


class RPCError(Exception):
    def __init__(self, code: int, message: str, data: Mapping[str, Any]) -> None:
        kind = data.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("RPCError data must contain a non-empty kind")
        if kind not in KNOWN_ERROR_KINDS:
            raise ValueError(f"unknown error kind {kind!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = dict(data)


def rpc_error(code: int, message: str, kind: str, **fields: Any) -> RPCError:
    return RPCError(code, message, {"kind": kind, **fields})


def no_snapshot(hint: str) -> RPCError:
    return rpc_error(-32000, "no snapshot", "no_snapshot", hint=hint)


def briefing_unresolved(bad_refs: list[str]) -> RPCError:
    return rpc_error(
        -32000,
        "briefing unresolved",
        "briefing_unresolved",
        bad_refs=list(bad_refs),
    )


def capability_revoked() -> RPCError:
    return rpc_error(-32000, "capability revoked", "capability_revoked")


def recipe_pin_mismatch(expected: str, found: str) -> RPCError:
    return rpc_error(
        -32000,
        "recipe pin mismatch",
        "recipe_pin_mismatch",
        expected=expected,
        found=found,
    )


def engine_stale(expected: str, found: str) -> RPCError:
    return rpc_error(
        -32000,
        "engine stale",
        "engine_stale",
        expected=expected,
        found=found,
    )


def harness_unavailable(harness: str) -> RPCError:
    return rpc_error(
        -32000,
        "harness unavailable",
        "harness_unavailable",
        harness=harness,
    )


def lock_timeout(lock: str) -> RPCError:
    return rpc_error(-32000, "lock timeout", "lock_timeout", lock=lock)


def indexer_unavailable(toolchain_code: str, detail: str) -> RPCError:
    # The code index is a must-have: a synchronous reconcile that can't run because the indexer
    # toolchain is unusable aborts adjudication rather than letting a fact predicate read a stale
    # view. Mirrors the build-tail hard stop (gtest.run_target -> failure="indexer_unavailable").
    return rpc_error(
        -32000,
        "indexer unavailable",
        "indexer_unavailable",
        toolchain_code=toolchain_code,
        detail=detail,
    )


def internal_error(exc: BaseException) -> RPCError:
    return rpc_error(
        -32603,
        "internal error",
        "internal_error",
        exception=type(exc).__name__,
        detail=str(exc),
    )

# engine-shared — `engine/arbiter_engine/shared/`

## Identity
The services both namespaces need: work-tree census, the lock inventory, the compile-db journal
consumer, and the build-driven indexing pipeline that joins runs (build) to facts (index).

## Components

### census
Generalized from cipher's SourceInventoryEntry hashing (sha256 + mtime_ns). Input: scope globs.
Output: `{digest, files, new[], deleted[], changed[]}` via direct tree walk — mtime/size
prefilter, sha256 confirm on suspects only; tens of ms warm at DBMS scale. Used by: build cache
validation (recipe `sources:`), goal memoization digests (folding toolchain hash + goal-spec
hash + recipe-book hash, ADR-0005), `arbiter/census` for the referee. Direct-walk semantics are
the point: new/deleted files are detected by construction (the snapshot-inventory blindness the
design review killed).

### locks
The single home of flock acquisition (ADR-0009): `match.lock` (Go-owned; listed for the map),
`snapshot.lock`, `overlay.lock`, `state.lock`, `build/<sha8(workdir)>.lock`. API: ordered
acquisition helpers with timeouts → typed `lock_timeout{lock}`; lock-order documented here and
asserted in debug builds (no ad-hoc flocks anywhere else in the engine).

### compile-db
Consumes the shim journal (`compile-journal.<build_id>.jsonl`): dedup by (src, out), expand
response files, normalize relative include/sysroot paths against entry cwd (cipher's rules),
emit/refresh `compile_commands.json` at the recipe's `compile_db.path`. Tolerates partial
journals (interrupted builds) idempotently. Falls back to the recipe's explicit `compile_db.target`
stage when interposition is off.

### pipeline (build-driven indexing, ADR-0004)
Orchestrates: tail journal during src_compile → semantic-key lookup in facts extract-cache →
schedule misses on the bounded extraction pool (cores/4 while compiler activity is detected,
full width after) → on build green: drain queue → facts merge + snapshot publish under
`snapshot.lock` → return `facts:{published, snapshot_id, files, warnings, extract_ms,
hidden_ms, tail_ms}` to the runner for the verdict. Failure honesty: per-file extraction
failures follow cipher policy and surface as `warnings`; a journal miss marker forces
`published:false` — the gear-up predicate fails closed.

## Invariants
stdlib-only; no daemon (pipeline lives within the EXEC engine's run call / startRun worker);
single-writer facts rule enforced here (publish path available only to player-QUERY context —
EXEC engines hand the drained queue's merge to the publish-side under lock with writer check);
all timing numbers measured, never estimated.

## Tests
census property tests (create/delete/touch/content-change matrices; glob edge cases); lock-order
violation detection; journal consumer vs torn/partial/duplicated lines; pipeline end-to-end on a
fixture build with a fake compiler (deterministic timings) asserting extract_ms/hidden_ms/tail_ms
accounting; publish-barrier failure modes (extraction failure → warnings; miss marker →
published:false).

## Done
census/locks in M4 (facts needs them), compile-db + pipeline in M6.

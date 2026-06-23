# Decisions (ADR log)

Append-only. Each entry is signed by the owner before any code that depends on it merges.
Format: number, date, status, decision, consequences. Spec changes happen ONLY through this file.

---

## ADR-0001 — Product name: Arbiter (2026-06-11, accepted)
"CERT" was the runner-up. Arbiter matches the spine: a referee who decides; the model only
requests adjudication. **Consequences:** binary `arbiter`, engine package `arbiter-engine`,
state dir `.arbiter/`, env prefix `ARBITER_`.

## ADR-0002 — Polyglot with one seam; engine stays Python (2026-06-11, accepted)
Go binary = chess kept nearly verbatim + engineclient + deploy + interpose. Python engine =
cipher-2 absorbed verbatim + crun rebuilt stdlib-only + shared/. cipher is NOT rewritten:
its ctypes runtime-located libclang loading is load-bearing (cgo would pin libclang at build
time or forfeit the static binary), and its hot paths are already native (clang, SQLite,
sha256, child processes). The one per-invocation hot hop — the per-TU compiler shim — lives
in the Go binary (`arbiter cc`). Escape hatch: golden transcripts pin engine behavior, so a
future leaf-by-leaf port is a refactor, not a re-verification. **Consequences:** golden
stdio-transcript contract tests are load-bearing CI forever; AST meta-test enforces the
stdlib-only boundary.

## ADR-0003 — gtest is the first-class harness (2026-06-11, accepted)
The real target is a gtest-guarded C DBMS. The postgres/sqlite sibling checkouts are dummy
benchmarks. pg_regress/TAP/exitcode adapters are optional M8+ work behind owner-signed
result-grammar ADRs; nothing on the critical path depends on them. **Consequences:** the
harness seam exists from day one, but only the gtest adapter ships in M5.

## ADR-0004 — Build-driven indexing; no standalone index ceremony (2026-06-11, accepted)
"Compile done ⇒ index done." The `arbiter cc` shim journals every compiler invocation
(compile-db for free from any build system; the journaled set IS the authoritative TU set)
and enqueues TU extraction overlapped with the build; the `src_compile` run verdict carries
`facts:{published, snapshot_id, extract_ms, hidden_ms, tail_ms}`. `arbiter index` survives
as CI/recovery plumbing only. **Consequences:** cipher's init/rebuild CLI ceremony is not a
user path; gear-up is a templated convention in every opening playbook.

## ADR-0005 — Two caches, two keys (2026-06-11, accepted)
Build cache keys on full flags + profile. Extraction cache keys on (TU content, include-closure
content, allowlist-cleaned semantic flags, toolchain id) — the allowlist strips codegen-only
flags (`-O*`, `-g*`, `--coverage`, `-fprofile-*`), so profile switches re-extract nothing.
`-fsanitize=*` is always kept in the key: sanitizers inject preprocessor state (`__SANITIZE_*`,
`__has_feature(*_sanitizer)`), so a sanitizer build never silently reuses plain-build facts.
`facts.key_flags` remains the user-confirmed opt-in for restoring sensitivity to the remaining
stripped dimensions (`-O*`/`-g*`); `/arbiter-intro` recommends it, never silently written into
committed config. Known blind spot: per-TU include closures are not yet wired — the publish
pipeline over-approximates with a single repo-wide headers digest (census walk of
`*.h/*.hh/*.hpp/*.hxx/*.inl` under root, excluding `.git`/`.arbiter`) folded into every unit's
include closure as `__repo_headers__`, so ANY header edit invalidates ALL cached units and
changes the snapshot id. That is correct but coarse: feature-flag header changes re-extract
everything rather than only their include-closure cone, until real per-TU closures land.
**Consequences:** memoization/cache digests fold
in toolchain hash, goal-spec hash, recipe-book hash; goal memoization ships default-off.

## ADR-0006 — Typed ResultSpec kinds run/fact; deny-self mcp guard (2026-06-11, accepted)
`{kind:"run"}` and `{kind:"fact"}` are the only sanctioned self-evaluation paths (they never
route through `.mcp.json`); any mcp-kind target resolving to `os.Executable()`
(LookPath+Abs+EvalSymlinks) is rejected `reserved_server` outright. mcp-kind gains closed
`expect[]` clauses (`eq|ne|ge|le|exists`, scalars, ≤8) for FOREIGN servers. `[Verify]` blocks
put named predicates in the playbook trust domain; executors invoke them by name.
**Consequences:** adversarial guard test matrix (symlinks, renamed binaries, argv injection)
is a merge gate for go-referee.

## ADR-0007 — Engine distribution: installed package; embedded mode opt-in (2026-06-11, accepted)
Default: pip/uv-installed `arbiter-engine` outside the target repo, pinned by machine-local
gitignored `.arbiter/run/engines.json` with typed staleness errors. Opt-in `--embedded-engine`
(air-gapped): go:embed unpack with sha256 digest verification at EVERY spawn, journaled, plus
Edit/Write deny rules — the adjudication evaluator is never silently model-patchable.

## ADR-0008 — Redaction is per-channel; journal keeps full fidelity (2026-06-11, accepted)
facts.jsonl / runs.jsonl follow cipher/crun strict redaction; `journal.jsonl` (0600, gitignored)
retains chess's full-fidelity forensics because the bypass-cost story depends on fully-logged
evidence. The one deliberate redaction exception, documented here.

## ADR-0009 — Locks and writers (2026-06-11, accepted)
Lock inventory under `.arbiter/locks/`: `match.lock`, `snapshot.lock`, `overlay.lock`,
`state.lock` (+ `BEGIN IMMEDIATE` for proven-lifecycle RMW), `build/<sha8(workdir)>.lock`.
Facts single-writer rule: only the player's QUERY engine reconciles/publishes overlays; all
engines read base + latest published overlay; fact-predicate evidence records
`{snapshot_id, overlay_id, view_state}`. Claim level: "DB-safe and build-serialized" until
8-way contention tests pass. `arbiter init` refuses network filesystems (typed error).
*Note (M4): the absorbed facts store keeps its own snapshot-writer mutex at
`.arbiter/facts/run/storage.lock` — that is the real path of the `snapshot.lock` named above
(force-broken via `recovery.force_unlock`); there is no `.arbiter/locks/snapshot.lock` on disk.*

## ADR-0010 — Companion diagnostic MCP servers ship inside arbiter-engine (2026-06-12, accepted)
`gdb-mcp` (structured GDB/MI debugging) and `perf-mcp` (C perf triage + measurement) are
**absorbed into the engine package** as `arbiter_engine.gdbmcp` / `arbiter_engine.perfmcp` —
the user installs arbiter + arbiter-engine and has both; there is no separate companion
distribution. They stay FOREIGN stdio MCP servers in the ADR-0006 sense: launched from
`.mcp.json` via the resolved engine interpreter (`python3 -m arbiter_engine.gdbmcp serve --root .`
/ `… perfmcp serve`), NEVER via the arbiter binary — so the deny-self `reserved_server` guard
keeps holding and mcp-kind predicates with `expect[]` clauses adjudicate their
`structuredContent` typed fields. They are not seats and not engine JSON-RPC namespaces: their
processes hold no referee or evaluator state. Their standalone `init` subcommands are dropped
(Go deploy owns all wiring); serve/doctor/scan/measure/probe/tools survive. `arbiter init`
probes `python3` + namespace importability; when the engine resolves it merges the two
`.mcp.json` entries (more precisely than "add-if-missing": an engine-generated entry is
*refreshed* on re-init so stale relative paths self-heal, while a FOREIGN same-name entry is
preserved untouched — ADR-0014 clarifies, and `TestInitRefreshesStaleEngineCompanionEntries`
pins it) and writes `.claude/agents/arbiter-debugger.md`, an executor-seat agent variant
wired with both companions. An absent engine degrades silently with a guidance hint.
**Consequences:** both namespaces obey the engine red lines (stdlib-only — AST-meta-test
enforced — repo-local state, no network); the debugger agent file is key-injected, 0600,
gitignored, deny-read like the other agents; default playbooks may direct gdb/perf evidence
gathering while adjudication stays typed (`expect[]` field comparison, never prose); the seat
RBAC boundary is unchanged — companion tools are host-level capabilities like Bash, not seat
tools; the source checkouts (`~/Project/gdb-mcp`, `~/Project/perf-mcp`) freeze for new features
— one-way import, the same posture as cipher-2's M4 import.

## ADR-0011 — One-command delivery: engine embedded, materialized at init (2026-06-12, accepted)
Owner verdict on UX: install is ONE command (`make install` → one binary), repo setup is ONE
command (`arbiter init`), and everything delivered must work instantly — no separate pip step,
no silently-skipped wiring discovered mid-flow. This amends ADR-0007's default posture: the
engine is embedded in the binary via go:embed, and `arbiter init` automatically materializes it
into repo-local `.arbiter/engine/` (digest-keyed, idempotent, `*.py` only) whenever no installed
`arbiter-engine` package resolves for `python3`. An installed package remains **preferred** when
importable (probed with a PYTHONPATH-scrubbed environment so a dev shell can't fake "installed").
Companion `.mcp.json` entries in embedded mode carry `env.PYTHONPATH=.arbiter/engine`; the
referee's mcp evaluation merges entry env **over** inherited env. ADR-0007's tamper-resistance
survives the flip: Edit/Write deny rules on `.arbiter/engine/**`, the tree is digest-tracked and
re-materialized on drift at init, and evaluator spawn-time digest verification still lands with
engineclient spawning (M4/M5) as signed there. The system prerequisite is exactly one: python3
≥ 3.9 (host gdb additionally for live debugging only). **Consequences:** `make install` is the
single install command; init reports which mode resolved; upgrading = reinstall the binary and
re-run init (digest change re-materializes); `.gitignore` gains `.arbiter/engine/`; ADR-0007's
`--embedded-engine` flag is subsumed by the automatic fallback.

## ADR-0012 — Starter openings ship with init; naming & predicate conventions (2026-06-12, accepted)
Owner verdict on the playbook library: names were pattern-chaos, content was generic prose a
capable model could ignore, and nothing actually shipped. Three fixes, all binding:
(1) **Delivery** — four starter openings are embedded in the binary and written by
`arbiter init` into `.arbiter/playbook/`, **refreshed to the shipped template on every init** so a
binary upgrade re-seeds the latest openings into existing repos; to customize, fork an opening to a
new name — own-named books outside the shipped `baseOpenings` set are never touched. (Corrects this
clause's original wording, "`.arbiter/match/playbook/` write-if-missing (user edits are sacred)":
the path has no `match/` segment and shipped openings are refreshed, not preserved — see ADR-0019.)
This complements, not replaces, the M7 intro's repo-specific openings (gear-up, gold-digger,
recipe-derivation — those need facts/runs).
(2) **Naming convention** (FORMAT.md, CI-linted) — a playbook name is the USER INTENT as an
imperative phrase: verb-first, kebab-case, ≤3 segments, file stem == name; the description
leads with "Use when …" and carries "Do not use … (use <other>)" cross-pointers so dedup
happens at curator-selection time. The four: `fix-reported-bug`, `hunt-latent-bugs`,
`build-feature`, `fix-slow-path`; the prior names from both parallel efforts (debug-repro-fix,
review-bug-hunt, feature-tdd, perf-triage-fix; debug, feature, review) are retired. The
design-canonical intro openings (freeplay, gold-digger, recipe-derivation, regression-triage)
are grandfathered and ship alongside.
(3) **Predicate discipline** — steps state the EXACT result predicate the executor must
submit (shell with explicit exit-code polarity, or mcp + `expect` clauses), and laws are
machine checks inside predicates (`git diff --quiet` untouchability, 5x determinism loops,
noise-band-beating measured gain), never prose. A playbook that does not wire the referee in
is not worth shipping. **Consequences:**
`TestStarterOpeningsFollowConventionAndRefreshOnInit` and `TestOpeningTemplateLint` are the
permanent lints; `/playbook-create` enforces the convention on user-authored books; the sibling
`arbiter-playbooks/` directory is a mirror of the embedded openings, no longer the delivery
channel.

## ADR-0013 — Retire the in-tree cipher-2 reference; the recorded corpus is the pin (2026-06-12, accepted)
Owner verdict: the full cipher-2 tree does not belong inside arbiter when a recorded corpus
achieves the same integrity goal. `import/cipher-2` (3.1 MB, commit "import: cipher-2 @main")
and the live A/B test (`test_facts_conformance.py`) are deleted. Before deletion the corpus
generator was flipped to record from the engine and PROVEN to reproduce the cipher-2-recorded
corpus byte-for-byte; `test_facts_conformance_corpus.py` remains the permanent byte-pin of the
frozen `search`/`detail` surface, and existing corpus lines are immutable. Two facts recorded
for future work: (1) the engine's `search`/`detail` are still STUBS (empty-corpus behavior
only) — the cipher-2 query/storage/extractor absorption (M4) is pending, and its source is
recoverable from this repo's import commit or the frozen upstream cipher-2 repo; (2) when
populated-snapshot behavior lands, new corpus scenarios must be cross-checked against upstream
cipher-2 OUT-OF-TREE before their recorded lines become the pin — the conformance discipline
survives the in-tree copy. **Consequences:** `import/` is gone; the corpus replay test and the
golden transcripts are the drift defenses; M4 work re-imports from upstream, not from a stale
in-tree copy.

## ADR-0014 — cwd is never load-bearing: explicit --root on every spawned entry (2026-06-12, accepted)
Field report: a match loaded by the curator subagent was invisible to the main-session player
("no active match"). Match state is file-shared (`.arbiter/match/run/state.json` under flock)
— the break was that every seat process derived the repo root from `os.Getwd()`, and the host
does not guarantee the spawn cwd of MCP servers (main session vs subagent contexts can differ;
the companion -32000 failure already proved cwd unreliability on real machines). Fix:
`arbiter serve <seat>` and `arbiter hook stop` accept `--root DIR` (resolved absolute; default
remains cwd for hand-run compatibility), and init writes `--root <abs repo>` into the player
`.mcp.json` entry, all five seat-agent templates, and the Stop-hook command. The stop-hook
claim matcher recognizes both the legacy `… hook stop` and rooted `… hook stop --root <dir>`
forms, so legacy deployments self-heal on re-init; `--remove` strips rooted entries and
engine-generated companion entries alike. **Consequences:** the cross-process regression test
(two real seat processes, hostile cwds, one shared root) is the permanent guard; after moving
a repo, re-run `arbiter init` (already the posture for the binary path); user-guide documents
"no active match" troubleshooting.

## ADR-0015 — The PreToolUse guard: playbook/match/engine paths are mechanically fenced (2026-06-12, accepted)
Owner verdict: "none of the agents (including the main agent) should be able to read playbook
files — I see no mechanism guaranteeing this block." Correct: the deployed `Read(...)` deny
rules gate only the Read tool; Bash `cat`, Grep, and Glob bypassed them freely. The mechanism
is now `arbiter hook guard` (internal/guard), wired by init as a PreToolUse hook with matcher
`Bash|Read|Edit|Write|NotebookEdit|Glob|Grep`: file tools are checked by resolved path, Bash
and glob/grep patterns by literal occurrence of the guarded paths (relative and root-absolute
forms). Guarded zones: `.arbiter/playbook/` (future steps must stay fenced),
`.arbiter/match/` (referee state and journal), `.arbiter/engine/` (the digest-verified
evaluator), `.claude/agents/arbiter-*` (credential-bearing seat files). Every denial returns a
TEACHING reason naming the sanctioned route (ShowStepJob / ReadPlayBook / AddPlayBook /
NotePlaybook / ListTask / ReviewTask / arbiter init). Posture matches the Stop gate: fail-open
on malformed input, deny on a hit; over-blocking a Bash command that merely mentions a guarded
path is accepted — the reason explains the correct route. Deny rules are still written
(defense in depth, now incl. Edit/Write on the same paths); `--remove` strips the guard like
every other generated entry. **Consequences:** the user-typed shell and editors are unaffected
(hooks fire on model tool calls only); the journal remains the forensics trail for anything
the guard cannot see; tool descriptions and error messages across the seat surface were
rewritten in the same change to carry next-action guidance — the deny reasons are part of
that same teaching contract.

## ADR-0016 — `[Checkpoint]` step type: a human-confirmation gate (2026-06-15, accepted)
*Records a spec change already shipped (PR #105), ratified after the fact.* A `[Checkpoint]`
step pauses the match for an explicit human pass/fail instead of dispatching executor work: the
player relays the step's question to the user (AskUserQuestion) and submits the result via the
player-seat tool `SubmitCheckpoint{decision:"pass"|"fail"}`. Pass advances the round, fail loops
the step, and the model cannot self-approve. A step carries tasks or a checkpoint, never both
(parser-enforced). **Consequences:** playbook tokens `[Checkpoint]`; player gains
`SubmitCheckpoint`; FORMAT.md and user-guide §5 document the gate.

## ADR-0017 — Result integrity: curated & step-bound predicates, frozen tests, subagent-stop gate (2026-06-15, accepted)
*Records a spec change already shipped (PRs #106–#109), ratified after the fact.* To remove the
submitter's ability to choose its own verdict, verifications live in the playbook trust domain.
`[Verify] <name>` predicates are snapshotted into match state at load; `[Submit] <name>` binds a
step to one (the executor must finish with `{verify:"<name>"}`), and `verify_policy: named`
forces every verdict through a curated predicate while the default `open` still permits inline
specs. `allow_overrides:["tests","options"]` opens only those fields of a curated `run` spec.
`RegisterTest{paths}` (executor) freezes test-source digests into match state; the async run
worker re-hashes the frozen sources at compile time and rejects a run whose compiled test bytes
drifted — closing the "pass round → weaken the frozen test → restore before poll" race a Go-side
content hash cannot see. `arbiter hook subagent-stop` adjudicates an executor subagent's
submission as a fail-open gate, and a build/harness failure or a zero-test run is `errored`,
never a passing verdict. **Consequences:** playbook token `[Submit]` + `allow_overrides`;
executor gains `RegisterTest`; match state carries `frozen_tests`, run payloads carry
`frozen_digests`; specialized executors (implementer, test-author) split write from execute so
the test-author authors tests without inheriting the player's framing.

## ADR-0018 — M4 facts absorption complete; `facts.incremental` goes live as a background index (2026-06-15, accepted)
The M4 absorption replaced the placeholder fact store/extractor with cipher-2's real engine:
content-addressed snapshots + SQLite read-index (the per-TU `extract-cache` of ADR-0005 is
**superseded** and removed — the absorbed extractor owns dirty re-extraction via
`extract_dirty_sources`), the libclang AST extractor, and the search/detail query layer.
Phase 2 makes `facts.incremental` a **live config section** (no longer a reserved bool) driving an
**automatic background index** (owner-required): an `IncrementalCoordinator` (`facts/incremental.py`)
that plans dirty sources (content + included-header fanout), re-extracts them, and publishes a
content-addressed temporary overlay (`overlay-<sha16>`) of fact upserts + source/relative tombstones
under `.arbiter/facts/run/incremental/`. Readers merge the published overlay at query time
(`store.open_view(overlay)`); the coordinator is the facts single-writer (player QUERY engine,
ADR-0009) and a session-resident poll thread keeps the overlay warm between the referee's
synchronous `arbiter/refresh` reconciles, so adjudication is never stale. Live knobs:
`poll_interval_ms`, `debounce_ms`, `overlay_ttl_seconds` (overlay GC — cipher-2 left this a no-op),
`max_dirty_files`; `worker_count` is unified with `facts.index_on_build.pool` (one knob for both
build-tail and incremental re-extraction). The coordinator keeps a real jsonl audit log
(`facts/log.py`) — the store/extractor stay log-disabled (forensics live in the referee journal).
**Consequences:** `facts.incremental` schema change (bool → section, validated knobs); new modules
`facts/incremental.py` + `facts/log.py`; `extract_cache.py` removed; cipher-2's incremental/overlay/
config tests migrate as acceptance (`engine/tests/c2`); the user-guide reserved-key note and
[m4-facts-absorption.md](proposals/m4-facts-absorption.md) §6 are updated. Full plan + decisions:
the M4 proposal §8.

## ADR-0019 — Review-driven doc/feature reconciliation (2026-06-15, accepted)
A code/docs review surfaced three classes of drift this batch reconciles, recorded here because the
relevant prose lives in append-only ADRs (0012, 0018) that must not be rewritten. **(1) Starter
openings are refresh-on-init, not write-if-missing.** `arbiter init` `atomicWrite`s every shipped
opening into `.arbiter/playbook/` on every run, so a binary upgrade re-seeds the latest templates —
correcting ADR-0012's "write-if-missing (user edits are sacred)" wording and its
`.arbiter/match/playbook/` path; the real path is `.arbiter/playbook/`. User customization is by
forking to a new name (`AddPlayBook`): own-named books are not in the `baseOpenings` list and `init`
never touches them. (`config.yml`/`recipes.yaml` remain write-if-missing — they hold user state.)
**(2) The four absorption gaps the review flagged are being wired in this batch**, completing what
ADR-0018 / the M4 proposal described: overlay-TTL GC (`overlay_ttl_seconds` is now consumed in the
incremental poll loop rather than a no-op), build-cache integration into the run path, the real
`runs.scan` handler, and facts-derived `TestBody` discovery. **(3) The accurate mechanisms of the
four starter openings** are recorded so future docs match the shipped templates:
`fix-reported-bug` = two plain `run` predicates (repro-runs-red, expect overall `failed`; suite-green,
expect overall `passed` / `max_failed` 0) plus a `RegisterTest` freeze — *not* a 5×-loop,
`git diff --quiet`, or a single predicate; `fix-slow-path` = a frozen complexity-ratio test
(`time(2N)/time(N)` under bound K), with the perf-mcp noise-band analysis-only; `hunt-latent-bugs` =
symptom-test polarity; `build-feature` = `build && ! run`. **Consequences:** ADR-0012 and ADR-0018
prose are superseded only where this entry states (path, refresh semantics, gap closure) and are
otherwise unchanged; `docs/design.md` seat-tool counts and the M4 proposals' test-dir path
(`engine/tests/c2/`) and headline total (24 files / 233 tests) are corrected in the same batch; the
four gap fixes land as their own reviewed PRs.

## ADR-0020 — Indexer toolchain config + mandatory-index hard stop (2026-06-17, accepted)
Two coupled owner decisions about the code index. **(1) `facts.toolchain` — an indexer-only
toolchain pin.** A new `config.yml` section (`clang`, `libclang`, `clang_args`) populates the
libclang extractor's `ExtractorConfig` and *only* the indexer — the build keeps running from the
recipe's own commands against the host PATH. It is wired into both `ExtractorConfig` construction
sites (`shared.pipeline.publish_after_build`, `facts.view._reconcile_extractor_config`) via
`relocation.extractor_toolchain_overrides` (fail-soft: missing/malformed config ⇒ no overrides).
There is deliberately **no gcc-binary key** — the indexer never executes gcc (`gcc_executable` only
feeds a cache key), so the gcc lever is `clang_args: [--gcc-toolchain=…]`. libclang is auto-derived
from the clang binary's `../lib` sibling; `libclang` overrides that only for nonstandard layouts.
**(2) The code index is a must-have — a broken indexer toolchain is a HARD STOP, unconditionally
(no opt-out).** The shared `_shim.TOOLCHAIN_FAILURE_CODES` (`clang_unavailable`,
`libclang_unavailable`, `libclang_version_mismatch`, `clang_capability_failed`) is enforced at both
points the toolchain is exercised: the build-tail publish re-raises them and `run_target` surfaces
`RunResult(overall="errored", failure="indexer_unavailable")`; the synchronous reconcile that gates
every fact predicate (`view.reconcile`, used by `arbiter/refresh` and writer `search`/`detail`)
maps them to the new `indexer_unavailable` SPEC error — so a match can never adjudicate on a missing
*or stale* index. Scope is toolchain-only: `no_compile_db` / journal-miss / non-green build /
non-toolchain `InitError`s stay graceful (nothing to index ≠ broken indexer). **Consequences:** both
background daemons (`view._background_loop`, the coordinator's `_poll_loop`) swallow per-tick so a
broken toolchain skips a tick rather than killing the engine — read-only querying over an
already-published index survives; `errors.SPEC_ERROR_KINDS` gains `indexer_unavailable`; tests that
drive a real build/reconcile must be hermetic (import the `c2` JSON-AST oracle + a fake toolchain).
Supersedes the prior "missing/incapable toolchain degrades to a typed not-published signal" wording
in engine-facts/engine-shared.

---

*Template for new entries:*

## ADR-NNNN — <title> (<date>, proposed|accepted|rejected)
<decision in 2-6 sentences. consequences as "**Consequences:** ...">

# Proposal: wire named [Verify] predicates + predicate-line comment grammar

Status: accepted (this branch implements it)

Two review findings against the M2–M8 branch are design gaps rather than
bugs, and need a deliberate mechanism:

1. Named `[Verify]` predicates are parsed, validated, stored on
   `Playbook.Verify`, and shipped in every opening template — but no
   runtime code reads them. Meanwhile `SubmitTask` lets the executor
   supply an arbitrary inline `ResultSpec`, so the adversarial seat picks
   its own win condition.
2. Predicate-line values run to end of line with no comment grammar, so
   `fact: symbol:foo # bar` silently changes the query (and the win
   condition) instead of either working or failing loudly.

## Part 1 — Named [Verify] predicates

### Intent

The opening templates already express the intended UX: checklists say
"Submit repro-fails", "Submit gear-up-published with the selected
profile". The curator authors predicates; the executor invokes them by
name; the referee resolves them from state the executor cannot touch.
This is the task-level analogue of the existing false-checkmate
protections (recipe pinning, goal predicates).

### Mechanism

**Reference kind.** `ResultSpec` gains a `verify` field
(`{"result": {"verify": "repro-fails"}}`). A reference is mutually
exclusive with every inline kind key; `verify.Validate` rejects mixed
specs. Submitting a reference resolves it to the curated spec before the
existing validate → recipe-pin → execute pipeline runs, so run-kind
named predicates still get pin-checked.

**Authority and snapshot.** `LoadPlayBook` copies the parsed
`Verify` map (and the policy, below) into match state, mirroring the
`RecipePin` trust model: editing the playbook file mid-match cannot swap
a predicate under an open round. Resolution happens inside the store
lock against the match snapshot, never against the file. Unknown names
fail with a typed `verify_not_found` error listing nothing (the executor
can read names from `ShowStepJob` output).

**Policy.** Frontmatter gains `verify_policy: open | named`
(default `open`). Under `named`, `SubmitTask` rejects inline specs with
a typed `verify_policy` error — every task verdict must come from a
curated predicate. `named` with zero `[Verify]` sections is a parse
error. Shipped openings whose checklists are fully name-driven
(`gold-digger`, `regression-triage`) move to `named`; `freeplay` stays
`open` by design (its premise is unconstrained predicates).

**Parameterization.** Curated specs are closed by default. A `[Verify]`
section may opt specific fields open with
`allow_overrides: ["tests", "options"]` (only those two values are
legal — expectation, kind, recipe, command can never be overridden).
A submission may then supply `tests`/`options` alongside `verify:`;
supplying an override the spec does not allow is a typed
`verify_override` error. This covers the templates' "with the selected
profile" parameterization without ceding authority over what passing
means.

**Goal aliasing.** `[SetGoal]` accepts `verify: <name>` as its sole
key, resolving to the named spec at end-of-parse (so section order does
not matter). This removes the current duplication where `gold-digger`'s
goal restates a near-copy of `repro-passes`.

**Surfacing.** `ShowStepJob` returns the available predicate names with
their kinds so the player can route work; the `SubmitTask` tool
description documents the `verify` field; the journal's
`task_submitted` entry records `verify` when a named predicate was used,
making the ledger say *which* curated proof backed each verdict.

**Out of scope (advisory only).** A parse-time lint cross-checking
checklist prose ("Submit <name>") against declared names was considered
and rejected: parse issues are load-blocking in this codebase, and
prose-pattern matching has false positives. Runtime `verify_not_found`
plus the `named` policy give the same safety without guessing at prose.

### Compatibility

Match-state additions (`verify_policy`, `verify_specs`) are new JSON
fields; old state files load with both absent → policy `open`, no named
specs, which is exactly today's behavior. Old playbooks parse unchanged.

## Part 2 — Predicate-line comment grammar

### Principle

Never strip by heuristic. A quote/JSON-aware "` #` is a comment"
stripper guesses, and every guess silently rewrites someone's value.
Instead: support full-line comments, and make every field whose grammar
provably excludes `#` fail **loudly with a hint** when one appears.

### Field-by-field audit

| field | grammar | `#` today | change |
|---|---|---|---|
| `shell` | verbatim command for `/bin/sh -c` | trailing `#` is a *shell* comment — self-correcting | verbatim, documented |
| `expect`/`arguments`/`tests`/`options` | JSON | already fails `json.Valid`/Unmarshal | append comment hint to error |
| `timeout_s`/`output_lines` | integer | already fails Atoi | append comment hint |
| `mcp` | `<server> <tool>` | already fails field-count check | append comment hint |
| `run` | recipe id, engine charset `[A-Za-z0-9_-][A-Za-z0-9._-]*` | rejected later by pin/engine with a confusing error | validate charset at parse, with hint |
| `fact` | whitespace-separated terms over symbols/paths | **silent corruption** — a `#…` term matches nothing, and absence-style expects can then pass vacuously | reject any term starting with `#`, with hint |

### Mechanism

1. **Full-line comments**: inside `[SetGoal]`/`[Verify]` sections, a
   line whose first non-space character is `#` is skipped (today it is
   a "not a key: value line" error). Documented in FORMAT.md for all
   sections.
2. **Comment hint**: the loud-failure messages above append
   "inline '#' comments are not supported; use full-line comments" when
   the offending value contains `#`, so the author's first error message
   names the actual mistake.
3. **`run` charset at parse**: mirror the engine's recipe-id rule so a
   bad id (comment or otherwise) fails at parse with a precise message
   instead of at pin/engine time.
4. **`fact` comment-term rejection**: terms are whitespace-delimited; a
   term *starting with* `#` cannot match any symbol/identifier and is
   rejected. A `#` embedded mid-term (e.g. a pathological path) remains
   legal — grammar-aware, not guessing.

### Rejected alternative

Stripping ` #` outside quotes: corrupts shell strings, JSON payloads,
and any future field whose grammar admits `#`; converts author intent
silently in exactly the cases that matter. Fail-closed costs one edit
and has no silent failure mode.

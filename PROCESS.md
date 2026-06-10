# PROCESS — how Arbiter gets built

Three roles, one spine.

## Roles

- **Implementer (GPT/Codex).** Writes code, tests, and module-doc updates, one issue at a time,
  delivered as PRs. Never merges. Never edits `docs/decisions.md` except to *propose* an entry.
  Standing prompt: `prompts/gpt-implementer.md`.
- **Reviewer (Claude, supervising).** Reviews every PR against the binding specs
  (`docs/modules/*.md`, `docs/design.md`), runs private acceptance scenarios, requests changes or
  approves. Merge posture is cautious by default: when evidence is ambiguous, the PR waits.
  The reviewer maintains the issue queue (cutting issues from `docs/migration.md` milestones,
  labeling `ready`) and escalates to the owner anything matching the escalation triggers below.
- **Owner (human).** Adjudicates `needs-decision` issues, signs `docs/decisions.md` entries,
  performs the owner-gate steps in `docs/migration.md` (subtree imports of the three source
  repos), and is the only role that can change a red line.

## The spine: issues → branches → PRs

1. Work exists only as GitHub issues. Milestone epics are cut into small issues labeled `ready`.
2. One issue → one branch (`issue/<n>-<slug>`) → one PR (`Closes #<n>`). One concern per PR;
   target ≤ ~400 net non-test lines. Stacked PRs allowed when declared in the PR body.
3. PR body format (mandatory): **WHAT** (one paragraph) / **WHY** (issue link) / **SPEC**
   (quote the governing module-doc section) / **TESTS** (what was added, what it proves) /
   **RISKS** (what could be wrong, what was NOT tested).
4. CI must be green before review: Go (`go vet`, `go test -race ./...`), Python
   (`python -m unittest`, stdlib-import AST meta-test), golden-transcript replay (both runtimes),
   plus any module-specific gates listed in the module doc.
5. Review verdicts: `approve` (reviewer merges), `request-changes` (implementer revises on the
   same branch), `needs-decision` (frozen until the owner signs an ADR), `needs-human`
   (anything touching security boundaries, adjudication semantics, or red lines — owner reviews
   the diff personally before merge).

## Spec authority

`docs/modules/*.md` are **binding**. `docs/design.md` is the design of record; on conflict,
the module doc wins for its module and the conflict itself must be raised as a `needs-decision`
issue. Code that needs the spec to change is blocked until a `docs/decisions.md` entry is merged
(owner-signed). No silent spec drift: a PR that changes behavior described in a module doc must
update that doc in the same PR.

## Red lines (violating any of these is an auto-reject)

1. The model never declares success: adjudication consumes typed verdict fields and counters only;
   no natural-language or string-pattern judging anywhere in referee or engine evaluators.
2. `arbiter-engine` is Python stdlib-only (enforced by the AST meta-test); the Go binary uses
   vendored deps only. New dependencies require an ADR.
3. cipher's facts red lines: typed libclang AST evidence only; explicit failure over degradation;
   no string-pattern symbol inference; `search`/`detail` request/response schemas byte-frozen.
4. Fail-closed posture everywhere except the two designed fail-open seams (the Stop hook and the
   `arbiter cc` shim's exec-through), which fail open for availability but journal every miss.
5. Repo-local state, stdio-only, no daemons, no network at runtime.
6. Seat RBAC is constructive: a tool not registered for a seat does not exist for that seat.

## Test discipline

- TDD: the failing test lands in the same PR as (or before) the fix/feature.
- Go: table-driven tests; the race detector is part of CI, not optional. Referee state-machine
  changes require scenario tests through the public two-phase protocol, never via internals.
- Python: stdlib `unittest`; hermetic fake harnesses (fake compilers, fake gtest binaries emitting
  structured output) — unit CI never invokes a real toolchain. Real-toolchain integration tests are
  a separate, explicitly-marked CI job.
- Golden transcripts (`testdata/transcripts/`): any change to JSON-RPC traffic shape regenerates
  transcripts in the same PR, and the regeneration diff is itself reviewed.
- **Withheld acceptance scenarios:** the reviewer maintains private end-to-end scenarios (real
  playbook matches against fixture repos). These are never shared with the implementer and never
  committed here. Green CI is necessary, not sufficient.

## Escalation (fixed format — no free-form escalations)

Open an issue labeled `needs-decision` containing exactly:

```
DECISION-REQUEST
blocking-issue: #<n>
question: <one sentence>
option-A: <one line> | cost: <one line>
option-B: <one line> | cost: <one line>
recommendation: <A|B> — <one line why>
spec-refs: <docs/modules/...#section, docs/design.md#section>
```

Then move on to the next `ready` issue. The owner's answer becomes a `docs/decisions.md` entry;
the reviewer relabels the blocked issue `ready` when the ADR merges.

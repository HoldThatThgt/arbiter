# Issue #129: System-Prompt Agent Guidance for Cipher

Accepted design draft for the agent-facing system prompt guide.

## README cornerstone

`cipher-2` is a FACT-only local runtime for C repositories. It extracts typed
Clang AST facts and `FactRelative` edges, stores them under `.cipher/`, and
exposes only local stdio MCP `search` and `detail`. This design does not add an
MCP tool, parameter, snapshot field, inference layer, parser fallback, or source
tree write. It adds a distributable agent-facing prompt guide so consuming
agents use the existing FACT surface correctly.

The guide must preserve the README's best-effort extraction boundary. Query
statuses describe the current indexed FACT snapshot and any temporary overlay;
they are not a claim that every source file in the target repository was fully
indexed. Skipped or partially accepted AST files remain observable through
`cipher2 status` / views and must be treated as coverage caveats by the
integrator.

## Problem

Weak models can learn relation syntax from tool descriptions, but they still
mis-handle high-fan-in relation results. In particular, `status="too_broad"` is
often treated as a failure to repair by grep, name guessing, or ad hoc source
parsing. Tool return messages and tool descriptions are not authoritative
enough to suppress that behavior.

The required behavior is prompt-policy behavior, not a new query primitive:
when cipher returns exact AST-backed relation counts and a bounded salient
subset within the indexed snapshot, the agent must report that bounded answer
and stop instead of trying to reconstruct the store.

## Decision

Use a hybrid distribution shape:

- Keep relation syntax, `status="ok"`, `status="needs_refinement"`, and
  `status="too_broad"` semantics in the MCP tool description and module docs.
- Add a canonical, copy-pasteable markdown prompt guide under `docs/` for
  integrators to pass as a system-prompt append file.
- Do not add repository-level `SKILL.md` or `AGENTS.md`; this is guidance for
  agents consuming cipher, not a maintenance rule for this repository.
- Do not auto-inject the guide from the MCP server. Integrators remain
  responsible for adding it to their agent system prompt, for example through
  an append-system-prompt file mechanism.

If wheel distribution needs to expose the same guide, add a package-data copy
only after the docs artifact is accepted, with a sync check so the docs copy and
packaged copy cannot drift. That packaging step must still avoid any runtime
tool or parameter change.

## Trust boundary

Adopt the explicit bounded-trust position:

- `status="ok"` means complete for the query and filters within the current
  indexed FACT view. It does not certify source-complete coverage when
  extraction warnings exist.
- `status="too_broad"` means the exact total and salient subset are
  authoritative within the current indexed FACT view. The agent should present
  the bounded answer and stop, but should not imply coverage beyond the current
  snapshot health.
- If source completeness matters, the integration should surface `cipher2
  status` warnings or require a clean rebuild/status check before asking the
  model to answer as source-complete.
- If snapshot health is degraded or unknown, the agent should report that caveat
  rather than use grep, name guessing, or a custom parser to patch a relation
  answer.

This keeps the core weak-model benefit: the prompt prevents models from
replacing cipher's AST-backed relation view with unreliable source-text
reconstruction, while still making the indexed-snapshot boundary visible.

## Prompt artifact contract

The prompt guide is second-person operational guidance for an agent. It must be
library-neutral and should contain these sections:

1. Why cipher beats grep: relation answers come from typed AST facts and edges;
   text search can miss mediated access patterns.
2. Query syntax: placeholder examples only, such as
   `readers:<Type.field>`, `writers:<Type.field>`,
   `accessors:<Type.field>`, `callers:<function>`,
   `callees:<function>`, and `file:<source-file>`.
3. Status handling:
   - `ok`: the returned set is complete for that query and filters within the
     current indexed snapshot.
   - `needs_refinement`: use returned candidates/examples to make one exact
     refinement; do not guess names repeatedly.
   - `too_broad`: the total count plus returned salient subset is the
     authoritative bounded answer within the current indexed snapshot; report
     the total and subset, then stop.
4. Prohibited fallbacks after relation results: do not grep for missing
   mediated accesses, do not enumerate `name:` guesses, and do not write a
   replacement parser.
5. Snapshot-health caveat: extraction warnings can mean the current snapshot has
   coverage gaps. If the integration exposes such warnings, mention them instead
   of silently upgrading an indexed-snapshot answer into a source-complete claim.
6. `detail` usage: use it for bounded payload, source context, and relation
   preview when starting from a fact, but prefer relation `search` for
   answerable relation subsets.
7. Default workflow: identify the relation, run one precise `search`, refine
   only when cipher asks for refinement, and answer from cipher-visible facts.

The guide may say that grep is useful for unrelated raw-text questions, but it
must be clear that grep is not a way to complete or audit a cipher relation
answer.

## Neutrality and leakage gate

The prompt guide must not contain concrete target-library names, source file
names, macro names, function names, type names, field names, or benchmark
answers. All examples must use placeholders or explicitly artificial names.

Acceptance must include a leakage scan over the prompt artifact. The denied
terms should be owned by the evaluation fixtures or benchmark manifest, not
embedded in the prompt text. The gate passes only when the scan returns zero
matches. This protects against training the evaluated model on the answer
shape of any specific repository.

## Documentation updates

After design approval, update documentation in this order:

- Move the accepted draft to `docs/design-drafts/YYYYMMDD-主题.md` and add it to
  `docs/design-drafts/README.md`; `docs/design_draft` is only the review
  staging path for this request.
- Add the prompt guide under `docs/`, with a name that makes it safe to pass
  directly as an append-system-prompt file.
- Update `docs/user-guide.md` to show where integrators should attach the
  guide, while preserving the existing stdio MCP setup. Cross-reference the
  existing MCP query section for `too_broad` bounded-answer semantics and the
  statement that complete relation audit is not a public MCP tool.
- Update `src/cipher2/mcp/README.md` to point from `search`/`detail` semantics
  to the system-prompt guide for agent behavior constraints.
- Update `docs/README.md` to index the new guide.
- If packaging the guide, update packaging docs and add a sync check between
  the docs artifact and packaged artifact.

The existing relation predicate docs remain the source for MCP response shape.
The new guide is only the model-behavior layer above those docs.

## Non-goals

- No new MCP public tool, private tool, parameter, or status value.
- No change to FACT extraction, storage schema, relation search, or ranking.
- No source-code fallback, generated parser, or grep-backed relation repair.
- No top-level maintenance skill or repository instruction file.
- No benchmark-specific examples in the delivered guide.

## Test and gate expectations

The implementation PR should be docs-first and should not touch runtime code
unless package-data inclusion is explicitly approved.

Required checks:

- Leakage scan over the prompt artifact returns zero matches.
- Placeholder examples are present for all relation predicates and filters.
- The guide contains explicit `ok`, `needs_refinement`, and `too_broad`
  handling.
- `ok` and `too_broad` wording is explicitly scoped to the current indexed
  snapshot, and does not claim source-complete coverage when extraction warnings
  exist.
- The guide explicitly forbids grep completion, name-guess enumeration, and
  replacement parser behavior for cipher relation answers.
- The guide points to the user guide for `too_broad` bounded-answer semantics
  instead of restating the full MCP response contract.
- Existing docs continue to state that MCP exposes only `search` and `detail`.

Manual validation may rerun weak-model A/B prompts using the guide as a system
prompt append file. That validation is evidence for the design, not a default
CI gate, because it depends on external models and credentials.

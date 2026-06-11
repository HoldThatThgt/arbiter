# How to use the `cipher` code-intelligence tools

Use this file as a system-prompt append for agents that consume cipher MCP
tools. It is usage guidance for the consuming agent, not a repository
maintenance rule.

## Trust Boundary

- Cipher answers come from the current indexed FACT snapshot and any active
  temporary overlay.
- `status="ok"` means complete for the query and filters within that indexed
  view.
- `status="too_broad"` with `complete=true` and `budget_exhausted=false` means
  the reported total and shown subset are authoritative within that indexed
  view.
- These statuses do not certify that every source file in the target repository
  was fully indexed. If your integration exposes extraction or snapshot-health
  warnings, mention that caveat instead of turning the answer into a
  source-complete claim.

## Why Cipher Beats Grep For Relations

For relationship questions, use cipher before grep. Cipher relation queries use
typed AST facts and relation edges. Text search can miss accesses or calls that
are mediated by macros, pointers, aliases, wrappers, generated declarations, or
other source forms where the literal text does not look like the semantic edge.

Do not use grep to complete, audit, or repair a cipher relation answer. If
snapshot health is degraded or unknown, report the health caveat or ask for a
fresh status/rebuild path; do not reconstruct cipher with source-text searches.

## Relation Query Syntax

Use relation predicates inside `search(query, limit)`:

| Goal | Query shape |
|---|---|
| Find functions that read a field | `readers:<field_object_id>` |
| Find functions that write a field | `writers:<field_object_id>` |
| Find functions that read or write a field | `accessors:<field_object_id>` |
| Find functions assigned to a function-pointer field | `dispatches_via:<field_object_id>` |
| Find callers of a function | `callers:<function>` |
| Find callees of a function | `callees:<function>` |
| Find bounded transitive callers/callees | `callers:<function> depth:2` or `callees:<function> depth:2` |
| Check bounded call reachability | `reachable:<start>-><target>` |
| Limit returned endpoints to a source file | `file:<source-file>` |

For field relations, first run a plain search for the field name plus owner
terms, choose the field result's exact `object_id`, then use that id as the
relation anchor. Do not synthesize owner-qualified field-name anchors. If
`writers:<field_object_id>` returns no writers, use `accessors:<field_object_id>`
when reads are an acceptable fallback, or call `detail(<field_object_id>)` to
inspect the field reader/writer buckets.

Use `name:<function>` or `caller:<function>` only when the endpoint name is
already known from the user request or a cipher response. Do not use those
filters to guess through candidate names.

## Status Rules

### `status="ok"`

The returned set is complete for the query and filters within the current
indexed snapshot. Report the returned results. Do not reread source files or run
grep just to verify the same relationship.

### `status="needs_refinement"`

Cipher has not selected one exact anchor. Use the returned candidates or
examples to make one precise refinement. Candidate messages include
`object_id`, owner, and source; choose one returned `object_id`. Do not spray
`name:` queries, synthesize owner-qualified field-name anchors, or invent
candidate names.

### `status="too_broad"`

The relation is too large to enumerate within the requested limit. The exact
`total` plus the returned salient subset is the bounded answer for the current
indexed snapshot when `complete=true` and `budget_exhausted=false`. Report both
the total and the subset, then stop.

If `complete=false` or `budget_exhausted=true`, do not turn the explored prefix
into a complete answer. Report that cipher hit its bounded depth or cost budget,
use the returned refinement example if one is available, and ask for a narrower
query such as a lower `depth` or a `file:` filter.

Do not grep for the missing tail. Do not enumerate `name:` guesses. Do not write
a parser or script to recreate cipher's relation analysis.

## Using `detail`

Use `detail(fact_id, budget)` when you already have a fact and need bounded
payload, source context, or relation preview. Treat `relative_preview` as a
preview: buckets are authoritative and can have `total_count`, `shown_count`,
and `truncated`; the top-level `relatives` list is only a small compatibility
sample. The serialized detail response is byte-capped by budget.

For answerable relation subsets, prefer relation `search` over manually walking
`detail` previews. For multi-hop call questions, use `callers:` / `callees:`
with `depth:<N>` or `reachable:<start>-><target>` instead of chaining one-hop
queries by hand; these call queries include direct calls and dispatch edges
composed from `dispatches_via` plus `assigned_to`. `reachable:` path nodes may
include a nullable `condition` field for the local branch or guard on that hop;
interpret a multi-hop path as the logical AND of its non-null hop conditions.

## Default Workflow

1. Identify whether the user is asking for a field relation, call relation,
   fact detail, multi-hop call closure, or reachability question.
2. For field relations, plain-search the field first and anchor follow-up
   relation queries by the returned field `object_id`.
3. Run one precise cipher `search` or `detail` query.
4. If cipher asks for refinement, refine with cipher-provided candidates or
   exact user-provided scope.
5. If cipher returns `ok`, answer from the returned set.
6. If cipher returns `too_broad`, answer with the exact total plus the shown
   salient subset and stop.
7. Mention snapshot-health caveats when the integration exposes them.

## Hard Stops

- Do not treat source grep as a relation-completion tool.
- Do not loop through guessed function, field, type, macro, or file names.
- Do not manually chain one-hop call queries when a bounded `depth:` or
  `reachable:` relation query answers the question.
- Do not write a replacement parser or analysis script.
- Do not claim source-complete coverage when the indexed snapshot has known or
  unknown health caveats.

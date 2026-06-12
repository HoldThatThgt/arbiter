# Issue #127: Bounded Transitive Call Search

## README Cornerstone

`cipher-2` v1 is a FACT-only local runtime. The public MCP surface remains
only `search` and `detail`; query execution reads existing `TheFact`,
`FactRelative`, source inventory, overlay state, and the persistent read index.

This design extends the existing #122 relation predicate grammar inside
`search.query`. It does not add a new MCP tool, a new MCP argument, user
configuration, Graph scope, inference rules, or synthesized edges. Transitive
answers use only stored `direct_call` relations.

## Problem

Current relation search can answer one-hop `callers:` and `callees:` queries.
Real development questions often need bounded multi-hop traversal or a yes/no
reachability check. Weak models can manually chain one-hop searches, but issue
#127 shows that a two-hop callees or reachability question can cost 25 to 30
tool calls and can be interrupted by name ambiguity during the manual chain.

The backend already has the direct-call edges and can collapse that loop into
one bounded BFS over fact ids.

## Decision

Extend relation-mode `search.query` with bounded call-graph traversal:

| Query | Semantics |
|---|---|
| `callees:<function>` | Existing one-hop outgoing `direct_call` query. |
| `callees:<function> depth:<N>` | Distinct functions reachable by outgoing `direct_call` within N hops. |
| `callers:<function>` | Existing one-hop incoming `direct_call` query. |
| `callers:<function> depth:<N>` | Distinct functions reachable by incoming `direct_call` within N hops. |
| `reachable:<A>-><B>` | Whether function A reaches function B within the built-in bounded search. |
| `reachable:<A>-><B> depth:<N>` | Same reachability query with an explicit bounded depth. |

`depth` defaults to `1` for `callers:` and `callees:`, preserving the current
one-hop meaning. Closure queries have a fixed internal maximum depth, initially
`3`, because fan-out grows quickly. `reachable:` has a separate fixed internal
maximum depth, initially `8`, because it stops on the first shortest-path hit
and otherwise only needs to answer whether the bounded search was exhausted.
Neither maximum is user-configurable.

Only `callers:`, `callees:`, and `reachable:` accept `depth` in this design.
Transitive `readers:` / `writers:` / `accessors:` are deferred. Invalid depth
syntax is deterministic:

- `depth` must be a positive base-10 integer.
- `depth:0`, negative values, non-numeric values, and repeated `depth:` terms
  return `status="needs_refinement"` with an executable corrected query.
- `depth` above the predicate-specific maximum returns `needs_refinement` with
  the supported maximum and a corrected example.
- `depth` on unsupported relation predicates, including `readers:`,
  `writers:`, and `accessors:`, returns `needs_refinement`; the response must
  not silently degrade to one-hop behavior.

## Anchor Resolution

Initial anchors reuse the existing function anchor rules from #122:

1. exact `object_id`;
2. exact function `object_name`;
3. same-kind text fallback.

More than one candidate returns `status="needs_refinement"` and does not run
BFS. A text-fallback-only match remains `needs_refinement` even if only one
candidate is found, matching the current relation-search contract.

`reachable:<A>-><B>` resolves both sides independently. If either side is
ambiguous, the response labels candidates as `start` or `target` and gives
examples using the more specific anchor.

Intermediate BFS nodes are never re-resolved by name. The traversal follows
stored endpoint ids from `direct_call` rows, so a manual-chain ambiguity such as
`callees:cmp_var` cannot interrupt a backend BFS. If an edge points at a
missing endpoint fact, skip that edge for display and traversal, increment a
bounded `skipped_missing_endpoint_count`, and continue.

## BFS Semantics

Storage performs BFS over `direct_call` edges:

- `callees` uses outgoing `direct_call` edges.
- `callers` uses incoming `direct_call` edges.
- The root function is marked seen at distance 0 and is not returned as an
  endpoint, even if a cycle reaches it later.
- Each endpoint appears once, at its shortest hop distance.
- Cycle handling is mandatory through a request-local `seen` set.
- Existing `file:`, `name:`, `caller:`, and bare-term filters apply to returned
  endpoints, not to frontier expansion. Filtering must not change reachability.
- Closure queries must compute an exact `total` for the bounded depth before
  applying `limit` only when depth and cost budgets are not exhausted.

`reachable:A->B` always traverses outgoing `direct_call` edges from A toward B.
It uses the same endpoint-id BFS and stops as soon as the target is found,
returning one shortest path. If the frontier is exhausted before the depth bound
and cost budgets are not exhausted, return `reachable=false` and
`complete=true`. If the depth bound is reached with unvisited frontier
remaining, return `reachable=false`, `complete=false`, and a message that the
target was not reached within the bounded depth; do not claim a global no-path
answer. If a cost budget is exhausted first, return `reachable=false`,
`complete=false`, `budget_exhausted=true`, and an executable refinement hint.

Every BFS has hard request-local cost budgets in addition to depth and `limit`:

- `visited_function_count` budget, initially 10,000 functions.
- `frontier_edge_count` budget, initially 50,000 scanned `direct_call` edges.

The budgets apply before endpoint filters and before `limit`. Exhausting either
budget stops traversal immediately. The response must expose which budget was
exhausted and suggest lowering `depth` or adding a narrowing filter such as
`file:`. These budgets are internal constants, not user configuration.

## Broad Results

Transitive closure can produce many endpoints. Relation-mode broad handling is
exact only when BFS completes within both depth and cost budgets:

- `total` is the exact number of distinct matched endpoints within the bounded
  depth after endpoint filters when `complete=true`.
- `limit` still controls only the returned subset and keeps its current `1..50`
  range.
- `total > limit` returns `status="too_broad"`, `truncated=true`, exact
  `total`, and the most salient bounded subset.
- Budget exhaustion returns `status="too_broad"`, `truncated=true`,
  `complete=false`, `budget_exhausted=true`, a non-exact
  `matched_endpoint_count`, and no exact `total`. It must not pretend the
  explored prefix is a complete count.
- The response never dumps the full graph.

Ranking is deterministic:

1. shortest `hop` first;
2. direct-call instance count descending for the endpoint at that hop;
3. unconditional edges before conditional edges; #133 landed through #138, so
   condition-capture is available and this tier is live for populated
   conditional `direct_call` edges;
4. endpoint `object_name`;
5. line-stripped endpoint source file;
6. endpoint `object_id`;
7. representative `relative_id`.

## Slim Relation Output

Relation-mode output should be slim before transitive results ship. This applies
to existing one-hop relation queries and the new transitive forms, and it
explicitly supersedes the #122 relation-mode output contract for `results` and
`top_by_salience`. The README migration PR must update both
`src/cipher2/mcp/README.md` and `src/cipher2/storage/README.md` in the same
change. Plain text FACT search and `detail.relative_preview` keep their current
shapes.

For relation-mode `results`, return endpoint rows shaped for enumeration:

```json
{
  "object_id": "fact:function:...",
  "object_name": "add_var",
  "object_source": "src/backend/parser/parse_expr.c:1234",
  "hop": 2,
  "relation_kind": "direct_call",
  "instances": 1
}
```

Do not include relation payload previews, source snippets, full endpoint
payloads, or duplicated full summaries. For `status="too_broad"`, `results` is
the salient subset; `top_by_salience` should be omitted or left empty for
relation mode rather than duplicating the same objects.

For `reachable`, return a compact path rather than endpoint enumeration:

```json
{
  "status": "ok",
  "query_kind": "relation_reachable",
  "reachable": true,
  "depth": 2,
  "complete": true,
  "path": [
    {"object_name": "in_range_numeric_numeric", "object_source": "..."},
    {"object_name": "intermediate", "object_source": "..."},
    {"object_name": "add_var", "object_source": "..."}
  ]
}
```

The path entries use the same slim fields. Relation edge details are limited to
`relation_kind="direct_call"` and hop order unless later evidence shows models
need more.

## Data and Implementation Boundary

No snapshot schema change is required. The existing read index already stores
facts and relatives with endpoint indexes. The implementation may extend
`RelationSearchQuery`, `RelationSearchResult`, and internal read-index methods,
but these remain storage/MCP internals, not public tools.

Base snapshot and temporary overlay views must have identical semantics. Overlay
visible `direct_call` edges participate in the same anchor resolution, BFS,
filtering, counting, ordering, and reachability path construction.

## Observability

Extend existing redacted `storage.search` / `mcp.search` relation counters:

- `query_kind=relation|relation_transitive|relation_reachable`
- `relation_predicate`
- `depth_requested`
- `depth_used`
- `depth_max`
- `anchor_candidate_count`
- `visited_function_count`
- `visited_function_budget`
- `frontier_edge_count`
- `frontier_edge_budget`
- `matched_endpoint_count`
- `total_is_exact`
- `returned_count`
- `too_broad_count`
- `budget_exhausted`
- `budget_exhausted_kind`
- `reachable_hit`
- `path_length`
- `skipped_missing_endpoint_count`

Do not log full query strings, source text, absolute target paths, payload
dumps, traceback text, or complete paths beyond bounded counters and the
existing query hash/preview policy.

## Documentation After Design Approval

- Move this draft into `docs/design-drafts/YYYYMMDD-bounded-transitive-search.md`
  and register it in `docs/design-drafts/README.md`.
- `src/cipher2/mcp/README.md`: document `depth:<N>`, `reachable:A->B`, slim
  relation rows, broad responses, and examples.
- `src/cipher2/storage/README.md`: document read-index BFS, exact bounded
  totals when budgets complete, non-exact budget exhaustion, cycle handling,
  overlay parity, and reachability completeness.
- `docs/user-guide.md`: add model-facing examples for two-hop callees/callers
  and reachability.
- `benchmarks/retrieval/README.md`: add weak-model multi-hop probes matching
  the T4/T5 class from issue #127.
- `tests/README.md`: register parser, storage BFS, MCP response, slim output,
  and overlay parity coverage.

## Test and Gate Expectations

TDD should cover:

- `callees:<function>` remains one-hop by default.
- `callees:<function> depth:2` returns distinct endpoints at hop 1 and hop 2
  with shortest-hop labels.
- `callers:<function> depth:2` uses the reverse direction.
- closure depth above the fixed maximum, depth zero, negative depth,
  non-numeric depth, repeated depth, and depth on unsupported predicates all
  return `needs_refinement`.
- cycles do not duplicate endpoints or return the root as an endpoint.
- ambiguous start anchors return `needs_refinement` without BFS.
- `reachable:A->B` returns `reachable=true` with one shortest path.
- `reachable:A->B` traverses outgoing calls from A to B and has a deeper maximum
  than closure queries.
- unreachable queries distinguish `complete=true` from bounded
  `complete=false`.
- `reachable` tests include a deep reachable path that exceeds the closure
  maximum but is inside the reachable maximum, plus a shallower user depth that
  returns `complete=false`.
- endpoint filters affect returned closure rows but not traversal.
- broad closure queries return exact `total`, `too_broad`, and slim rows only
  when cost budgets are not exhausted.
- high-fanout closure tests exhaust the visited-function or frontier-edge budget
  and assert `complete=false`, `budget_exhausted=true`, no exact `total`, and an
  executable narrowing hint.
- closure ranking puts unconditional `direct_call` endpoints before conditional
  endpoints when hop and instance count are equal.
- relation-mode rows omit payload previews and avoid `results` /
  `top_by_salience` duplication.
- base view and overlay view produce the same semantics.
- no new MCP tool, no new MCP argument, no user config, no Graph scope, and no
  snapshot schema change.

Implementation PRs should run the normal unittest suite plus storage and MCP
performance gates, including a high-fanout traversal gate for the BFS budgets.
Retrieval acceptance should rerun the issue #127 T4/T5 style probes and verify
that the weak model no longer needs manual multi-hop BFS.

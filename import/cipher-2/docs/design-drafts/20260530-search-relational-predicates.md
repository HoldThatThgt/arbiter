# Issue #122: Relational Search Predicates and Actionable Refinement

## README cornerstone

`cipher-2` v1 is a FACT-only local runtime. The public MCP surface remains only `search` and `detail`; runtime queries read existing `TheFact`, `FactRelative`, source inventory, and the persistent read index. This design therefore extends `search.query` semantics to join across existing relation edges. It does not add a new MCP tool, does not add a public `relations` tool, and does not infer or synthesize edges.

## Problem

High-fan-in fields now have real `field_read` / `field_write` coverage, but `detail.relative_preview` is intentionally bounded. For fields such as `NullableDatum.value`, showing all hundreds of accessors would violate the bounded preview contract. A weak model can notice `truncated=true` and `total_count`, but that weak signal does not reliably make it refine the query.

The missing capability is a backend-executable way to ask for related facts along an edge, then narrow by file or endpoint name:

```text
readers:NullableDatum.value file:numeric.c
writers:NullableDatum.value caller:numeric_add
callers:add_var file:parse_expr.c
callees:add_var
```

## Decision

Add relation-aware predicates to the existing `search.query` string. Prefer query parsing over new MCP parameters so `tools/list` still exposes only `search(query, limit)` and `detail(fact_id, budget)`.

Supported relation predicates:

| Predicate | Anchor kind | Relation join | Returned endpoint |
|---|---|---|---|
| `readers:<field>` | field | incoming `field_read` | reader function |
| `writers:<field>` | field | incoming `field_write` | writer function |
| `accessors:<field>` | field | incoming `field_read` + `field_write` | accessor function |
| `callers:<function>` | function | incoming `direct_call` | caller function |
| `callees:<function>` | function | outgoing `direct_call` | callee function |

Supported filters after a relation predicate:

| Filter | Semantics |
|---|---|
| `file:<substring>` | casefold substring match on the returned endpoint source file after stripping a trailing `:<positive-line>` from repository-relative `object_source` |
| `name:<substring>` | casefold substring match on the returned endpoint `object_name` |
| `caller:<substring>` | unconditional synonym of `name:`; accepted for model ergonomics and reproducibility |
| bare terms | existing AND text match against the returned endpoint searchable fields |

`condition:` is intentionally deferred from v1. Matching serialized condition JSON/text would expose an internal representation and make deterministic behavior depend on serialization details the owner did not request for this design.

For `file:`, endpoint source file normalization is the same rule already used by `detail.relative_preview`: parse `object_source` from the right; if it ends in `:<positive integer>`, match against the prefix before that suffix. For example, `ruleutils.c:9647` matches `file:ruleutils.c`; malformed suffixes use the full `object_source` unchanged.

The first relation predicate in a query selects relational mode. Multiple relation predicates in one query are rejected with a refinement error rather than interpreted as nested joins.

## Anchor resolution

The anchor value after `:` is resolved through existing FACT identity and search data:

1. Exact `object_id` match if the value is a fact id.
2. Exact field owner alias for `Owner.field` / `Owner::field` when the predicate requires a field.
3. Exact `object_name` for the required anchor kind.
4. Existing text search fallback restricted to the required anchor kind.

Anchor candidates are deduplicated by `object_id`. Exactly one candidate is accepted. Zero anchors returns normal empty results with `status="ok"`. More than one candidate returns `status="needs_refinement"` and does not run the relation join.

`needs_refinement.anchor_candidates` is ordered deterministically by:

1. resolution tier in the order listed above;
2. exact `object_name` before non-exact text fallback;
3. line-stripped endpoint source file;
4. full `object_source`;
5. `object_id`.

The response returns the first bounded candidate set plus examples that use a more specific anchor, such as `readers:NullableDatum.value`.

## Result shape

`SearchResponse` keeps existing fields and adds explicit status/refinement fields:

```json
{
  "status": "ok",
  "query_kind": "relation",
  "relation": "readers",
  "anchor": {"object_id": "...", "object_name": "value"},
  "total": 12,
  "result_count": 12,
  "truncated": false,
  "results": [
    {
      "object_id": "fact:function:...",
      "object_name": "numeric_add",
      "object_source": "src/backend/utils/adt/numeric.c:1234",
      "matched_relations": [
        {"relation_kind": "field_read", "instances": 3, "representative_relative_id": "..."}
      ]
    }
  ]
}
```

For compatibility, each item remains a fact summary. `matched_relations` is an additive bounded context field available only for relational search results. For `accessors:`, one endpoint may contain both `field_read` and `field_write` matches.

## Too broad response

Relational mode must compute `matched_endpoint_count` after anchor resolution, relation join, endpoint grouping, and all filters, before applying `limit`. `limit` controls only how many salient endpoint summaries are returned in `results` / `top_by_salience`; it does not control the count used to decide status.

If `matched_endpoint_count <= limit`, return `status="ok"`, `total=matched_endpoint_count`, and `truncated=false`. If `matched_endpoint_count > limit`, return `status="too_broad"`, exact `total=matched_endpoint_count`, `truncated=true`, and actionable refinement hints. This removes the dead zone where 30 matches at default `limit=20` would otherwise degrade to the weak `truncated=true` signal this design is meant to replace. The MCP `limit` range remains `1..50`.

```json
{
  "status": "too_broad",
  "query_kind": "relation",
  "relation": "readers",
  "total": 663,
  "message": "663 matching readers is too broad to enumerate; add a file or caller/name filter.",
  "available_filters": ["file:<path>", "caller:<function>", "name:<function>"],
  "examples": [
    "search('readers:NullableDatum.value file:numeric.c')",
    "search('readers:NullableDatum.value file:numeric.c caller:numeric_add')"
  ],
  "top_by_salience": ["...bounded fact summaries..."],
  "results": ["...same bounded head for compatibility..."],
  "truncated": true
}
```

The response still includes an exact `total` and a bounded salient head. It does not dump all edges. If filters are already present but the result is still too broad, examples preserve the existing filters and add the next useful dimension.

Plain text FACT search keeps the existing `truncated` behavior unless a separate counted search path is designed later. This issue gates the stronger `too_broad` contract on relation searches, where exact relation counts are already available from the relation index.

## Ranking

Relational search returns endpoint facts grouped by endpoint id. Ordering is deterministic and reuses the relative preview salience model:

1. relation tier, with `field_write` before `field_read` for `accessors:`;
2. grouped `instances` descending;
3. unconditional relations before conditional relations;
4. resolved endpoint before missing endpoint;
5. endpoint `object_name`;
6. endpoint source file;
7. representative `relative_id`.

The query never ranks by fixed `confidence=1.0`. It never scans source text or infers relationships from names.

## Data and implementation boundary

No snapshot schema change is required. The existing read index already stores facts and relatives; implementation may add an internal storage method such as `relation_search(...)` that performs a SQLite join from anchor relation rows to endpoint fact rows. That method is not an MCP tool.

All state is request-local. Base snapshot and temporary overlay views must have identical semantics. Overlay-visible facts and relatives participate in the same anchor resolution, join, filtering, counting, and ranking.

## Implementation sequence

Implement A before B:

1. First land the relational search substrate: parser, anchor resolution, relation join, endpoint grouping, `file:` / `caller:` filters, deterministic counts, ranking, and tests for `status="ok"` queries.
2. Then land the broad-response layer that converts `matched_endpoint_count > limit` into `status="too_broad"` with exact totals, salient head, available filters, and executable examples.

The second step must not be implemented without the first, because the refinement examples must be backend-executable.

## Observability

Extend existing `mcp.search` / `storage.search` logs with redacted counters only:

- `query_kind=relation`
- `relation_predicate`
- `anchor_candidate_count`
- `matched_endpoint_count`
- `returned_count`
- `too_broad_count`
- `filter_count`

Do not log full query strings beyond the existing bounded preview/hash behavior. Do not log source text, absolute target paths, relation payload dumps, or conditions beyond bounded counters.

## Recursive documentation updates after design approval

- Move this working draft to `docs/design-drafts/20260530-search-relational-predicates.md` and register it in `docs/design-drafts/README.md`.
- `src/cipher2/mcp/README.md`: document relation predicates, additive response fields, `too_broad`, and examples.
- `src/cipher2/storage/README.md`: document internal relation search semantics over the read index and overlay parity.
- `docs/user-guide.md`: show model-facing examples for `readers:` / `writers:` / `callers:` and refinement after `too_broad`.
- `benchmarks/retrieval/README.md`: add high-fan-in FIELD_ACC probes that use relational queries after a broad prompt.
- `tests/README.md`: register parser, relational search, broad-response, and overlay parity coverage.

The migration PR must explicitly state that this design supersedes the #118 conclusion that high `total_count` plus `truncated=true` is an acceptable complete answer for high-fan-in field questions. #118 remains valid for bounded `detail.relative_preview`, but the GLM-5.1 A/B evidence shows weak models need a prominent, executable refinement path in `search`.

## Test and gate expectations

TDD should cover:

- `readers:Owner.field file:path.c` returns only functions with incoming `field_read` to that field and endpoint source matching the file filter.
- `file:ruleutils.c` matches an endpoint with `object_source=".../ruleutils.c:9647"` by stripping the trailing line suffix before matching.
- `writers:` and `accessors:` preserve relation kind context and deduplicate by endpoint.
- `callers:` and `callees:` traverse `direct_call` in the correct direction.
- ambiguous anchors return `needs_refinement` when candidate count is greater than one, with deterministically ordered candidates and executable examples.
- relation queries with `matched_endpoint_count > limit`, including counts below 50, return `status="too_broad"`, exact `total`, available filters, examples, and a bounded salient head.
- adding `file:` and then `caller:` / `name:` can move the same query to `status="ok"`.
- `caller:` and `name:` are unconditional synonyms and produce the same deterministic result ordering.
- plain search still follows existing term-AND behavior.
- no new MCP tool, no new MCP input parameter, no snapshot schema change.

Run the normal unittest suite plus MCP/storage performance gates for the implementation PR. Retrieval acceptance should rerun the high-fan-in FIELD_ACC cases and the weak-model A/B check: the broad response must drive the model toward backend-executable refinements such as `search('readers:NullableDatum.value file:numeric.c')`.

| issue | status(done/blocked/question) | commit | note |
| --- | --- | --- | --- |
| #18 | done | engine-core: start async run worker (#18) | Red: method_not_found for arbiter/startRun; transcripts regenerated twice; full gate green. |
| #19 | done | go-engineclient: complete fault-tolerant client (#19) | Red: client.ToolsList undefined; timeout poison/respawn and protocol fault tests green under -race. |
| #20 | question | go-engineclient: expand transcript corpus v1 (#20) | Red: initialize not found in corpus stems; Go+Python replay green, engineclient -race green, full gate green. REVIEWER-QUESTION: M2 exit asks for seat-spawned engine child/tools-list forwarding, but current #20 scope is transcript corpus and seat has no engineclient integration yet. |
| #34 | done | verify: add mcp expect clauses (#34) | Red: mcp spec must not set expect; closed ops, scalar validation, reports, and full gate green. |
| #35 | done | verify: harden reserved-server guard (#35) | Red: hardlink returned no reserved_server; symlink/path/hardlink/argv matrix and full gate green. |
| #38 | done | playbook: add verify grammar (#38) | Red: Playbook missing Capabilities/Verify fields; [Verify], typed predicates, capabilities, and full gate green. |
| #36 | done | match: pin recipes at load (#36) | Red: Match missing RecipesPin and recipe_pin_mismatch code; load pin, run mismatch journal, and full gate green. |
| #37 | done | match: add async run goals (#37) | Red: CheckStepJobOutput missing RunID; startRun/runStatus goal polling, false-checkmate kill test, and full gate green. |
| #21 | done | shared: add worktree census (#21) | Red: cannot import shared.census; create/delete/touch/content/glob tests, arbiter/census, transcripts twice, and full gate green. |
| #22 | done | shared: centralize lock inventory (#22) | Red: cannot import shared.locks / undefined Go lock APIs; ordered lock helpers, match.lock migration, meta-checks, and full gate green. |
| #23 | question | interpose: add adversarial cc matrix (#23) | Red: arbiter cc is not implemented; matrix skip-gated only until #24 to preserve green per-commit gates. REVIEWER-QUESTION: Is ARBITER_REQUIRE_CC=1 acceptable as the forced-red mode for the tests-before-implementation split? |
| #24 | question | interpose: implement arbiter cc shim (#24) | Red: arbiter cc is not implemented; #23 matrix forced green, full gate green. REVIEWER-QUESTION: The current repo has no CI benchmark harness for the 3ms startup p95 claim; should that be a separate gate issue? |
| #25 | done | shared: emit compile database (#25) | Red: cannot import shared.compile_db; journal dedup, response expansion, partial tolerance, fallback generator, and full gate green. |
| #26 | done | runs: parse recipe book v2 (#26) | Red: cannot import runs.recipes; strict YAML subset, profiles/compile_db/targets, golden corpus, portability checks, and full gate green. |
| #27 | done | runs: create sqlite state schema (#27) | Red: cannot import runs.state; WAL/busy_timeout schema, run_test occurrence, BEGIN IMMEDIATE, proven lifecycle, async initializer, and full gate green. |
| #30 | done | runs: validate build cache by census (#30) | Red: cannot import runs.build_cache; no-sources miss, clean census hits, edit/new/delete misses, restart persistence, and full gate green. |

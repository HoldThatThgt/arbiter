| issue | status(done/blocked/question) | commit | note |
| --- | --- | --- | --- |
| #18 | done | engine-core: start async run worker (#18) | Red: method_not_found for arbiter/startRun; transcripts regenerated twice; full gate green. |
| #19 | done | go-engineclient: complete fault-tolerant client (#19) | Red: client.ToolsList undefined; timeout poison/respawn and protocol fault tests green under -race. |
| #20 | question | go-engineclient: expand transcript corpus v1 (#20) | Red: initialize not found in corpus stems; Go+Python replay green, engineclient -race green, full gate green. REVIEWER-QUESTION: M2 exit asks for seat-spawned engine child/tools-list forwarding, but current #20 scope is transcript corpus and seat has no engineclient integration yet. |
| #34 | done | verify: add mcp expect clauses (#34) | Red: mcp spec must not set expect; closed ops, scalar validation, reports, and full gate green. |

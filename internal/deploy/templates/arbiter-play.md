---
name: arbiter-play
description: Select an Arbiter opening and run the refereed loop for a user request.
---

You are the player seat: you analyze and dispatch, you never execute. Your nine tools
are the referee's surface (ShowStepJob, CreateTask, CheckStepJob, ListTask, ReviewTask,
NotePlaybook, AddPlayBook, search, detail); all editing, building, and testing happens
inside executor subagents you dispatch with the Task tool.

## Opening — exactly two moves

1. Task tool → subagent `arbiter-curator`, prompt = the user request verbatim plus
   "select and load the most specific opening; fall back to freeplay".
2. On its report (opening name + entry step) → enter the loop. If it reports even
   freeplay is missing, tell the user to re-run `arbiter init` and stop.
   Never read `.arbiter/playbook` or `.arbiter/match` from disk — your entire view of
   the match comes through the tools.

## The loop — repeat until terminal

1. **ShowStepJob {}** → the current step's job, checklist, gotchas (pitfalls earlier
   matches hit on THIS step — weave the relevant ones into your task prompts), and
   open tasks.
2. **Gear-up steps:** derive the build profile from the request before dispatching —
   memory corruption/UAF/leak → "asan"; races/locking → "tsan" if the recipe has it,
   else "debug"; coverage/test-gap work → "coverage"; otherwise "debug". Preserve
   request-named feature flags exactly.
3. **Orient fact-first.** Before dispatching edits, call
   search {"query": "<symbol or relation>"} (relations: callers:<fn>, callees:<fn>,
   writers:<field id>, reachable:<a>-><b>, depth:2 variants) and
   detail {"fact_id": "<id>", "budget": "small"}. Keep the object ids — they become
   fact_refs. An empty/no-snapshot result before the first gear-up build is normal.
4. **CreateTask** — one per work item, decomposed so each maps to checklist items:
   {"request": "<self-contained instruction: goal, scope limits, the exact result
   predicate or [Verify] name the executor must submit>",
    "fact_refs": ["<object ids from step 3>"]}   (≤8; the referee resolves them into
   briefing cards the executor reads via ReviewTask). Record each returned task_id.
   A bad ref fails the call with briefing_unresolved — fix the ref, don't drop it.
5. **Dispatch each task** with the Task tool. Route by work type:
   - crash / memory corruption / wrong runtime values / perf → `arbiter-debugger`
   - write or prove tests (repro, symptom, scenarios) → `arbiter-test-author`
   - make tests pass / scoped source change → `arbiter-implementer`
   - anything else → `arbiter-executor`
   Every dispatch prompt MUST contain, verbatim labeled lines:
     task id: <task_id>
     task: <the CreateTask request text>
     finish: call SubmitTask with this task id; result must be <predicate/[Verify] name>
   Independent tasks dispatch in parallel (one message, multiple Task calls).
6. **When executors return:** ShowStepJob {} for the task ledger. Any fail or
   suspicious pass → ReviewTask {"task_id": "<id>"} and read the per-clause
   expect_report (path/op/value/actual). Fixable → re-dispatch the SAME task_id with
   the failing clause quoted; structural (wrong step assumption) → proceed to
   adjudication and let the opening's failure branch handle it.
7. **NotePlaybook** {"step_id": "<current>", "note": "<one sentence>"} the moment you
   hit a pitfall worth one sentence (environment quirk, hidden precondition, misleading
   failure) — not at the end, when you'll have forgotten. Skip notes that restate
   existing gotchas.
8. **CheckStepJob {}** and act on its answer:
   - complete=false, reason no_tasks → you skipped step 4: create the tasks.
   - complete=false, reason open_tasks → executors still owe submissions: wait for
     dispatched ones or re-dispatch the listed task_ids.
   - complete=false, reason goal_running → the checkmate goal is executing
     asynchronously; continue the loop and CheckStepJob again later.
   - complete=true → loop from 1 (new step), or finish when the match reports a
     terminal state (checkmate / success / failure / aborted).

## Endgame

ListTask {} for the full ledger; ReviewTask anything you will cite. Report to the
user: opening name, terminal status with its reason (checkmate / steps_exhausted /
stop_limit …), one line per round, and the key verification evidence (predicates +
verdicts, not your impressions). Backfill any unrecorded gotchas via NotePlaybook —
the match being over does not close the notebook.

## Iron rules

- While a match is active, do not end your reply: the Stop gate will block you; when
  it does, return to the loop (ShowStepJob → tasks → CheckStepJob) until terminal.
- You have no LoadPlayBook and no SubmitTask — never simulate either with file edits
  or prose. Executors missing/failing to spawn is user-facing news, not something to
  work around by doing the task yourself.
- Never self-assess success. The referee adjudicates typed predicates; your job is to
  feed it adjudicable tasks.

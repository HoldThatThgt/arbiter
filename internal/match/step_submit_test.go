package match

import (
	"context"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

const submitBoundBook = `---
name: bound
description: step-bound flow
---

[Verify] strong
shell: exit 0

[Verify] weak
shell: exit 0

[STEP] only
[StepJob]
do it
[CheckList]
- item
[Submit] strong
[Branch]
success: END
failure: only
`

// A [Submit]-bound step takes the predicate choice away from the model: only
// the bound curated predicate is accepted — not an inline spec, not a different
// (weaker) curated predicate. This is what stops the player from dictating a
// trivially-true `[result]` through the executor.
func TestSubmitTaskStepBoundPredicate(t *testing.T) {
	root := repoWithBook(t, "bound.md", submitBoundBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("bound"); err != nil {
		t.Fatal(err)
	}

	// ShowStepJob surfaces the binding so the player knows what to dispatch.
	show, err := store.ShowStepJob()
	if err != nil {
		t.Fatal(err)
	}
	if show.Step == nil || show.Step.Submit != "strong" {
		t.Fatalf("ShowStepJob Submit = %#v, want strong", show.Step)
	}

	task, err := store.CreateTask("t")
	if err != nil {
		t.Fatal(err)
	}

	// Inline spec → rejected (the model cannot author the predicate).
	_, err = store.SubmitTask(context.Background(), task.TaskID, "s", "r",
		verify.ResultSpec{Kind: "shell", Command: "exit 0"})
	if code := toolCode(err); code != playbook.CodeStepSubmitMismatch {
		t.Fatalf("inline spec: code = %q, want %q (err=%v)", code, playbook.CodeStepSubmitMismatch, err)
	}
	// A different curated predicate → rejected (the model cannot even pick which).
	_, err = store.SubmitTask(context.Background(), task.TaskID, "s", "r",
		verify.ResultSpec{Verify: "weak"})
	if code := toolCode(err); code != playbook.CodeStepSubmitMismatch {
		t.Fatalf("wrong curated: code = %q, want %q (err=%v)", code, playbook.CodeStepSubmitMismatch, err)
	}
	// The bound predicate → accepted, runs, passes.
	out, err := store.SubmitTask(context.Background(), task.TaskID, "s", "r",
		verify.ResultSpec{Verify: "strong"})
	if err != nil {
		t.Fatalf("bound predicate rejected: %v", err)
	}
	if out.Verdict != TaskPass {
		t.Fatalf("verdict = %q, want pass", out.Verdict)
	}
}

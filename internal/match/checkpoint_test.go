package match

import (
	"context"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

const checkpointBook = `---
name: checkflow
description: checkpoint flow
---

[STEP] confirm
[StepJob]
draft, then ask the user
[Checkpoint]
Do the drafted scenarios capture the feature you want?
[Branch]
success: END
failure: confirm
`

func TestCheckpointStepGate(t *testing.T) {
	root := repoWithBook(t, "checkflow.md", checkpointBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("checkflow"); err != nil {
		t.Fatal(err)
	}

	// ShowStepJob surfaces the checkpoint question (the player must ask the user).
	show, err := store.ShowStepJob()
	if err != nil {
		t.Fatal(err)
	}
	if show.Step == nil || show.Step.Checkpoint != "Do the drafted scenarios capture the feature you want?" {
		t.Fatalf("checkpoint not surfaced: %#v", show.Step)
	}

	// Before a decision: not complete, reason awaiting_checkpoint.
	if out, err := store.CheckStepJob(context.Background()); err != nil || out.Complete || out.Reason != "awaiting_checkpoint" {
		t.Fatalf("pre-checkpoint = %#v err=%v", out, err)
	}
	// CreateTask is rejected on a checkpoint step.
	if _, err := store.CreateTask("x"); toolCode(err) != playbook.CodeCheckpoint {
		t.Fatalf("CreateTask on checkpoint: code = %q", toolCode(err))
	}
	// Only pass/fail are accepted.
	if _, err := store.SubmitCheckpoint("maybe"); toolCode(err) != playbook.CodeCheckpoint {
		t.Fatalf("bad decision: code = %q", toolCode(err))
	}

	// pass → success branch → END → match finishes successfully.
	if _, err := store.SubmitCheckpoint(TaskPass); err != nil {
		t.Fatal(err)
	}
	out, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !out.Complete || out.Match != StatusFinishedSuccess {
		t.Fatalf("post-pass = %#v", out)
	}
}

func TestCheckpointFailLoops(t *testing.T) {
	root := repoWithBook(t, "checkflow.md", checkpointBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("checkflow"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitCheckpoint(TaskFail); err != nil {
		t.Fatal(err)
	}
	out, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// failure branch loops back to confirm (a fresh round on the same step).
	if !out.Complete || out.NextStep != "confirm" {
		t.Fatalf("post-fail = %#v", out)
	}
	// The fresh round has no decision yet.
	if got, err := store.CheckStepJob(context.Background()); err != nil || got.Reason != "awaiting_checkpoint" {
		t.Fatalf("fresh round = %#v err=%v", got, err)
	}
}

// SubmitCheckpoint on a task step is rejected — the two adjudication surfaces
// do not cross.
func TestSubmitCheckpointRejectedOnTaskStep(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitCheckpoint(TaskPass); toolCode(err) != playbook.CodeCheckpoint {
		t.Fatalf("SubmitCheckpoint on task step: code = %q", toolCode(err))
	}
}

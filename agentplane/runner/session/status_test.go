package session

import (
	"context"
	"path/filepath"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
)

func TestObservationRetainsCheckpointAndDoesNotExposePointers(t *testing.T) {
	ctx, root, _, store, manager := runnerTestManager(t, nil, func(string) string { return "destination" })
	s := runnerTestReserve(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "turn"}), nil); err != nil {
		t.Fatal(err)
	}
	observed := s.Observe()
	attached := observed.Attached()
	status, _ := observed.Status()
	if attached.ActiveTurnId != status.GetActiveTurnId() || !status.Busy || len(observed.AutosuspendBlockers()) != 1 {
		t.Fatalf("inconsistent projection: %v, %v", attached, status)
	}
	attached.Spec.Model = "mutated"
	*status.ActiveTurnId = "mutated"
	if err := s.Emit(ctx, observation(&eventpb.TurnCompleted{TurnId: "turn", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}), nil); err != nil {
		t.Fatal(err)
	}
	again := observed.Attached()
	if again.ActiveTurnId != "turn" || again.LastCursor != attached.LastCursor || again.Spec.Model == "mutated" {
		t.Fatalf("observation changed: %v", again)
	}
	idle, _ := s.Observe().Status()
	if idle.Busy || idle.ActiveTurnId != nil {
		t.Fatalf("completed Session remains busy: %v", idle)
	}
}

func TestObservationRetainsIdleBarrierBetweenAdmissionAndBatchCommit(t *testing.T) {
	ctx, root, _, store, manager := runnerTestManager(t, nil, func(string) string { return "destination" })
	s := runnerTestReserve(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	if _, err := s.Admit(ctx, &commandpb.Command{CommandId: "command", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}}, false); err != nil {
		t.Fatal(err)
	}
	before := s.journal.LastCursor()
	translated, release := make(chan struct{}), make(chan struct{})
	defer close(release)
	done := make(chan error, 1)
	go func() {
		done <- s.journal.WithBatch(ctx, func(ctx context.Context) error {
			if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "turn"}), nil); err != nil {
				return err
			}
			if err := s.ConfirmUserMessage(ctx, harness.UserMessageConfirmation{HarnessMessageID: "message", OriginCommandIDs: []string{"command"}, TurnID: "turn", Text: "hello"}, nil); err != nil {
				return err
			}
			close(translated)
			select {
			case <-release:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		})
	}()
	select {
	case <-translated:
	case <-ctx.Done():
		t.Fatal("translation did not reach the commit gate")
	}
	observed := s.Observe()
	status, _ := observed.Status()
	attached := observed.Attached()
	if attached.LastCursor != before || attached.ActiveTurnId != "" || status.ActiveTurnId != nil {
		t.Fatalf("uncommitted turn escaped attachment history: %v, %v", attached, status)
	}
	blockers := observed.AutosuspendBlockers()
	if !status.Busy || len(blockers) != 1 || blockers[0].Reason != autosuspendActiveTurn {
		t.Fatalf("pending-to-active transition lost idle barrier: %v, %v", status, blockers)
	}
	// Release the batch; the deferred close also unblocks it if an assertion fails.
	release <- struct{}{}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("native batch did not commit")
	}
	committed := s.Observe()
	status, _ = committed.Status()
	if committed.Attached().LastCursor <= before || status.GetActiveTurnId() != "turn" || !status.Busy {
		t.Fatalf("committed turn absent: %v", status)
	}
}

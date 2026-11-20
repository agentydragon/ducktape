package session

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

func TestObservationRetainsCheckpointAndDoesNotExposePointers(t *testing.T) {
	ctx, root, _, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	if err := s.Emit(ctx, harness.Observation(&eventpb.TurnStarted{TurnId: "turn"}), nil); err != nil {
		t.Fatal(err)
	}
	observed := s.Observe()
	attached := observed.Attached()
	host := observed.HarnessHostSession()
	blockers := observed.AutosuspendBlockers()
	if attached.ActiveTurnId != host.GetActiveTurnId() || host.GetActiveTurnId() != "turn" || !hasBusyReason(blockers, autosuspendActiveTurn) || len(blockers) != 1 {
		t.Fatalf("inconsistent projection: attached=%v host=%v blockers=%v", attached, host, blockers)
	}
	attached.Spec.Model = "mutated"
	host.ActiveTurnId = "mutated"
	if err := s.Emit(ctx, harness.Observation(&eventpb.TurnCompleted{TurnId: "turn", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}), nil); err != nil {
		t.Fatal(err)
	}
	again := observed.Attached()
	if again.ActiveTurnId != "turn" || again.LastCursor != attached.LastCursor || again.Spec.Model == "mutated" {
		t.Fatalf("observation changed: %v", again)
	}
	idle := s.Observe()
	if idle.HarnessHostSession().GetActiveTurnId() != "" || len(idle.AutosuspendBlockers()) != 0 {
		t.Fatalf("completed Session remains busy: host=%v blockers=%v", idle.HarnessHostSession(), idle.AutosuspendBlockers())
	}
}

func TestObservationRetainsIdleBarrierBetweenAdmissionAndBatchCommit(t *testing.T) {
	ctx, root, _, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	if _, err := s.Admit(ctx, &commandpb.Command{CommandId: "command", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}}, false); err != nil {
		t.Fatal(err)
	}
	pending := s.Observe()
	if pending.HarnessHostSession().GetPendingCommands() != 1 || pending.HarnessHostSession().GetActiveTurnId() != "" || !hasBusyReason(pending.AutosuspendBlockers(), autosuspendPendingCommand) {
		t.Fatalf("pending command status = host %v blockers %v, want a pending-command reason without an active turn", pending.HarnessHostSession(), pending.AutosuspendBlockers())
	}
	before := s.journal.LastCursor()
	translated, release := make(chan struct{}), make(chan struct{})
	defer close(release)
	done := make(chan error, 1)
	go func() {
		done <- s.journal.WithBatch(ctx, func(ctx context.Context) error {
			if err := s.Emit(ctx, harness.Observation(&eventpb.TurnStarted{TurnId: "turn"}), nil); err != nil {
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
	attached := observed.Attached()
	host := observed.HarnessHostSession()
	if attached.LastCursor != before || attached.ActiveTurnId != "" || host.GetActiveTurnId() != "" {
		t.Fatalf("uncommitted turn escaped attachment history: %v, %v", attached, host)
	}
	blockers := observed.AutosuspendBlockers()
	if len(blockers) != 1 || blockers[0].GetReason() != autosuspendActiveTurn {
		t.Fatalf("pending-to-active transition lost idle barrier: %v, %v", host, blockers)
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
	if committed.Attached().LastCursor <= before || committed.HarnessHostSession().GetActiveTurnId() != "turn" || !hasBusyReason(committed.AutosuspendBlockers(), autosuspendActiveTurn) {
		t.Fatalf("committed turn absent: host=%v blockers=%v", committed.HarnessHostSession(), committed.AutosuspendBlockers())
	}
}

func hasBusyReason(blockers []*environmentlifecyclepb.AutosuspendBlocker, reason string) bool {
	for _, blocker := range blockers {
		if blocker.GetReason() == reason {
			return true
		}
	}
	return false
}

package session

import (
	"context"
	"fmt"
	"path/filepath"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
)

func TestSessionReconcileSnapshotsIgnoreJournalOnlyChanges(t *testing.T) {
	ctx, root, resources, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	inputPointer := lifecycle.ResourceFor(s.id, ReconcileSnapshot{}).Metadata()
	before, err := resources.Get(ctx, inputPointer)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Emit(ctx, observation(&eventpb.HarnessStderr{Text: "raw output"}), nil); err != nil {
		t.Fatal(err)
	}
	s.wake()
	after, err := resources.Get(ctx, inputPointer)
	if err != nil {
		t.Fatal(err)
	}
	if !before.Metadata().Version().Equal(after.Metadata().Version()) {
		t.Fatal("journal output republished lifecycle input")
	}
	s.requestExecution()
	demand, _, err := ReadReconcileSnapshot(ctx, resources, s.id)
	if err != nil || demand.Demand != 1 {
		t.Fatalf("execution demand lost: %v, %v", demand, err)
	}
	s.mu.Lock()
	s.reconciliations = append(s.reconciliations, &pendingReconciliation{})
	s.mu.Unlock()
	s.wake()
	inspection, _, err := ReadReconcileSnapshot(ctx, resources, s.id)
	if err != nil || inspection.Inspections != 1 {
		t.Fatalf("inspection work absent from Session snapshot: %v, %v", inspection, err)
	}
	s.mu.Lock()
	s.reconciliations = nil
	s.mu.Unlock()
	s.wake()
	inspection, _, err = ReadReconcileSnapshot(ctx, resources, s.id)
	if err != nil || inspection.Inspections != 0 {
		t.Fatalf("inspection work did not settle in Session snapshot: %v, %v", inspection, err)
	}
}

// Only dispatch readiness is needed by this publication test.
type dispatchReadinessAdapter struct {
	harness.Adapter
	blocked bool
}

func (a *dispatchReadinessAdapter) BlocksCommandDispatch() bool { return a.blocked }

func TestSessionInputTracksNativeDispatchReadinessWithoutJournalEvent(t *testing.T) {
	ctx, root, resources, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	adapter := &dispatchReadinessAdapter{blocked: true}
	s.mu.Lock()
	s.adapter = adapter
	s.mu.Unlock()
	s.wake()
	before := s.journal.LastCursor()
	adapter.blocked = false
	s.wake()
	input, _, err := ReadReconcileSnapshot(ctx, resources, s.id)
	if err != nil || input.DispatchBlocked {
		t.Fatalf("raw reply did not release dispatch: %v, %v", input, err)
	}
	if s.journal.LastCursor() != before {
		t.Fatal("readiness test unexpectedly emitted an Event")
	}
}

func TestSessionDemandPublicationRetriesFromCurrentDemand(t *testing.T) {
	ctx, root, resources, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")

	conflictingState := &sessionDemandConflictCoreState{CoreState: resources}
	manager.state = state.WrapCore(conflictingState)
	manager.publishSession(s, true)

	input, found, err := lifecycle.Read[ReconcileSnapshot](ctx, manager.state, s.id)
	if err != nil || !found {
		t.Fatalf("read Session reconcile snapshot after conflict retry: found=%t error=%v", found, err)
	}
	if input.Demand != 2 {
		t.Fatalf("Session demand after a competing increment and one request = %d, want 2", input.Demand)
	}
	if !conflictingState.conflicted || conflictingState.inputUpdateAttempts != 2 {
		t.Fatalf("conflict retry state = conflicted %t, update attempts %d, want true and 2", conflictingState.conflicted, conflictingState.inputUpdateAttempts)
	}
}

func TestSessionDemandPublicationStartsFromZeroWhenInputIsMissing(t *testing.T) {
	ctx, root, resources, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	if err := resources.Destroy(ctx, lifecycle.ResourceFor(s.id, ReconcileSnapshot{}).Metadata(), state.WithDestroyOwner(lifecycle.EnvironmentOwner)); err != nil {
		t.Fatal(err)
	}

	s.requestExecution()

	input, found, err := ReadReconcileSnapshot(ctx, resources, s.id)
	if err != nil || !found {
		t.Fatalf("read recreated Session reconcile snapshot: found=%t error=%v", found, err)
	}
	if input.Demand != 1 {
		t.Fatalf("Session demand after recreating a missing input = %d, want 1", input.Demand)
	}
}

type sessionDemandConflictCoreState struct {
	state.CoreState
	conflicted          bool
	inputUpdateAttempts int
}

func (s *sessionDemandConflictCoreState) Update(ctx context.Context, value resource.Resource, opts ...state.UpdateOption) error {
	if value.Metadata().Type() == ReconcileSnapshotType {
		s.inputUpdateAttempts++
		if !s.conflicted {
			s.conflicted = true
			current, err := s.CoreState.Get(ctx, value.Metadata())
			if err != nil {
				return fmt.Errorf("read competing Session reconcile snapshot: %w", err)
			}
			input, ok := current.(*typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]])
			if !ok {
				return fmt.Errorf("unexpected Session reconcile snapshot type %T", current)
			}
			input.TypedSpec().Demand++
			if err := s.CoreState.Update(ctx, input, opts...); err != nil {
				return fmt.Errorf("write competing Session demand: %w", err)
			}
			latest, err := s.CoreState.Get(ctx, value.Metadata())
			if err != nil {
				return fmt.Errorf("read Session reconcile snapshot after competing demand: %w", err)
			}
			return inmem.ErrVersionConflict(value.Metadata(), value.Metadata().Version(), latest.Metadata().Version())
		}
	}

	return s.CoreState.Update(ctx, value, opts...)
}

func TestJournalNotificationsOnlySignalBlockerTransitions(t *testing.T) {
	ctx, root, _, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	// Establish the initial notification predicates before observing transitions.
	s.notifyBlockers()
	var notifications atomic.Int32
	s.deps.changed = func() { notifications.Add(1) }
	for _, text := range []string{"first", "second"} {
		if err := s.Emit(ctx, observation(&eventpb.HarnessStderr{Text: text}), nil); err != nil {
			t.Fatal(err)
		}
		s.wake()
	}
	if notifications.Load() != 0 {
		t.Fatal("raw history woke aggregate observers")
	}
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "turn"}), nil); err != nil {
		t.Fatal(err)
	}
	if notifications.Load() != 1 {
		t.Fatal("active turn did not notify observers")
	}
	if err := s.journal.WithBatch(ctx, func(ctx context.Context) error {
		if err := s.Emit(ctx, observation(&eventpb.TurnCompleted{TurnId: "turn", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}), nil); err != nil {
			return err
		}
		if notifications.Load() != 1 {
			t.Error("idle notification escaped before turn completion committed")
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	s.wake()
	if notifications.Load() != 2 {
		t.Fatal("committed idle transition did not notify observers")
	}
}

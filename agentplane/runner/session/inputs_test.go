package session

import (
	"path/filepath"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
)

func TestSessionInputsIgnoreJournalOnlyChanges(t *testing.T) {
	ctx, root, resources, store, manager := runnerTestManager(t, nil, func(string) string { return "destination" })
	s := runnerTestReserve(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	inputPointer := componentResource(s.id, Input{}).Metadata()
	drainPointer := componentResource(s.id, DrainInput{}).Metadata()
	before, err := resources.Get(ctx, inputPointer)
	if err != nil {
		t.Fatal(err)
	}
	beforeDrain, err := resources.Get(ctx, drainPointer)
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
	afterDrain, err := resources.Get(ctx, drainPointer)
	if err != nil {
		t.Fatal(err)
	}
	if !before.Metadata().Version().Equal(after.Metadata().Version()) || !beforeDrain.Metadata().Version().Equal(afterDrain.Metadata().Version()) {
		t.Fatal("journal output republished lifecycle input")
	}
	s.requestExecution()
	demand, _, err := ReadInput(ctx, resources, s.id)
	if err != nil || demand.Demand != 1 {
		t.Fatalf("execution demand lost: %v, %v", demand, err)
	}
	unchangedDrain, err := resources.Get(ctx, drainPointer)
	if err != nil {
		t.Fatal(err)
	}
	if !afterDrain.Metadata().Version().Equal(unchangedDrain.Metadata().Version()) {
		t.Fatal("dispatch demand woke aggregate drain")
	}
	s.mu.Lock()
	s.reconciliations = append(s.reconciliations, &pendingReconciliation{})
	s.mu.Unlock()
	s.wake()
	drain, _, err := ReadDrainInput(ctx, resources, s.id)
	if err != nil || !drain.Inspecting {
		t.Fatalf("inspection drain dependency absent: %v, %v", drain, err)
	}
	s.mu.Lock()
	s.reconciliations = nil
	s.mu.Unlock()
	s.wake()
	drain, _, err = ReadDrainInput(ctx, resources, s.id)
	if err != nil || drain.Inspecting {
		t.Fatalf("inspection drain did not settle: %v, %v", drain, err)
	}
}

// Only dispatch readiness is needed by this publication test.
type dispatchReadinessAdapter struct {
	harness.Adapter
	blocked bool
}

func (a *dispatchReadinessAdapter) BlocksCommandDispatch() bool { return a.blocked }

func TestSessionInputTracksNativeDispatchReadinessWithoutJournalEvent(t *testing.T) {
	ctx, root, resources, store, manager := runnerTestManager(t, nil, func(string) string { return "destination" })
	s := runnerTestReserve(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	adapter := &dispatchReadinessAdapter{blocked: true}
	s.mu.Lock()
	s.adapter = adapter
	s.mu.Unlock()
	s.wake()
	before := s.journal.LastCursor()
	adapter.blocked = false
	s.wake()
	input, _, err := ReadInput(ctx, resources, s.id)
	if err != nil || input.DispatchBlocked {
		t.Fatalf("raw reply did not release dispatch: %v, %v", input, err)
	}
	if s.journal.LastCursor() != before {
		t.Fatal("readiness test unexpectedly emitted an Event")
	}
}

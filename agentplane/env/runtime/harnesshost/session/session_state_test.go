package session

import (
	"context"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/journal"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

type gatedRecordState struct {
	state.State
	entered chan struct{}
	release chan struct{}
	calls   atomic.Int32
}

func (s *gatedRecordState) UpdateWithConflicts(ctx context.Context, ptr resource.Pointer, update state.UpdaterFunc, options ...state.UpdateOption) (resource.Resource, error) {
	if s.calls.Add(1) == 1 {
		close(s.entered)
		<-s.release
	}
	return s.State.UpdateWithConflicts(ctx, ptr, update, options...)
}

func TestConcurrentModelChangesKeepCachedAndDurableOrder(t *testing.T) {
	ctx := context.Background()
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	gated := &gatedRecordState{State: resources, entered: make(chan struct{}), release: make(chan struct{})}
	store, err := retained.NewStore(t.TempDir(), gated)
	if err != nil {
		t.Fatal(err)
	}
	record := retained.Record{Harness: sessionpb.Harness_HARNESS_CLAUDE, CWD: "/workspace", Model: "old", EventSourceID: "source"}
	if err := store.Write("session", record); err != nil {
		t.Fatal(err)
	}
	s := newSession(sessionDependencies{ctx: ctx, state: gated, store: store, changed: func() {}}, "session", &record, openTestJournal(t))
	for _, id := range []string{"first", "second"} {
		command := &commandpb.Command{CommandId: id, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: id}}}
		if _, _, err := s.journal.Admit(ctx, command, ""); err != nil {
			t.Fatal(err)
		}
	}
	first := make(chan error, 1)
	go func() { first <- s.ModelChanged(ctx, "first", "one", nil) }()
	<-gated.entered
	if s.recordMu.TryLock() {
		s.recordMu.Unlock()
		close(gated.release)
		t.Fatal("record update lock released before the COSI write finished")
	}
	second := make(chan error, 1)
	secondStarted := make(chan struct{})
	go func() {
		close(secondStarted)
		second <- s.ModelChanged(ctx, "second", "two", nil)
	}()
	<-secondStarted
	close(gated.release)
	if err := <-first; err != nil {
		t.Fatal(err)
	}
	if err := <-second; err != nil {
		t.Fatal(err)
	}
	if gated.calls.Load() != 2 {
		t.Fatalf("COSI updates = %d, want 2", gated.calls.Load())
	}
	persisted, err := store.Read("session")
	if err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	cached := s.record.Model
	s.mu.Unlock()
	if persisted.Model != "two" || cached != "two" {
		t.Fatalf("model durable=%q cached=%q, want two", persisted.Model, cached)
	}
}

func TestSessionEnvironmentCarriesItsOwnDestination(t *testing.T) {
	globalEnvironment := map[string]string{"TZ": "UTC"}
	want := map[string]string{
		"session-1": `{"destination_ref":{"namespace":"test","name":"sandbox","uid":"sandbox-uid"},"session_id":"session-1"}`,
		"session-2": `{"destination_ref":{"namespace":"test","name":"sandbox","uid":"sandbox-uid"},"session_id":"session-2"}`,
	}
	ctx, root, _, store, manager := newSessionTestManager(t, globalEnvironment, func(id string) string { return want[id] })
	for id := range want {
		s := reserveTestSession(t, ctx, manager, store, id, filepath.Join(root, id), "")
		if got := s.Session().Environment["AGENTPLANE_DESTINATION"]; got != want[id] {
			t.Fatalf("%s destination = %q, want %q", id, got, want[id])
		}
	}
	if _, shared := globalEnvironment["AGENTPLANE_DESTINATION"]; shared {
		t.Fatal("a Session's destination leaked into the Host-wide harness environment")
	}
}

func TestSessionBlockersTrackBusyReasonsAndReturnToIdle(t *testing.T) {
	ctx, root, resources, store, manager := newSessionTestManager(t, nil, func(string) string { return "test-destination" })
	s := reserveTestSession(t, ctx, manager, store, "session-1", filepath.Join(root, "project"), "setup script")
	if err := publish(ctx, resources, s.ID(), LifecycleStatus{Phase: Preparing}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Admit(ctx, &commandpb.Command{
		CommandId: "command-1",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}},
	}, false); err != nil {
		t.Fatal(err)
	}
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "turn-1"}), nil); err != nil {
		t.Fatal(err)
	}

	assertSessionBlockers(t, s, []string{"ActiveTurn", "PendingCommand", "Activation", AutosuspendSetup})
	status, _ := s.Status()
	if !status.Busy || status.ExecutionState != sessionpb.SessionExecutionState_SESSION_EXECUTION_STATE_PREPARING {
		t.Fatalf("busy session status = %+v, want preparing and busy", status)
	}

	if err := s.FailCommand(ctx, "command-1", "test completed", nil); err != nil {
		t.Fatal(err)
	}
	if err := s.Emit(ctx, observation(&eventpb.TurnCompleted{TurnId: "turn-1", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}), nil); err != nil {
		t.Fatal(err)
	}
	if err := s.Emit(ctx, observation(&eventpb.SetupFinished{ExitCode: 0}), nil); err != nil {
		t.Fatal(err)
	}
	if err := publish(ctx, resources, s.ID(), LifecycleStatus{Phase: Dormant}); err != nil {
		t.Fatal(err)
	}

	assertSessionBlockers(t, s, nil)
	status, _ = s.Status()
	if status.Busy || status.ExecutionState != sessionpb.SessionExecutionState_SESSION_EXECUTION_STATE_DORMANT {
		t.Fatalf("idle session status = %+v, want dormant and not busy", status)
	}
}

func newSessionTestManager(t *testing.T, environment map[string]string, destination func(string) string) (context.Context, string, state.State, *retained.Store, *Manager) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	root := t.TempDir()
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	store, err := retained.NewStore(filepath.Join(root, "sessions"), resources)
	if err != nil {
		t.Fatal(err)
	}
	manager, err := NewManager(withTestSessionFacilities(t, Config{
		Context: ctx, State: resources, Store: store, WorkspaceRoot: root,
		Destination: destination, HarnessEnvironment: environment,
		PublishSeal: func(string, *journal.SealState) error { return nil },
		PublishAck:  func(string, retained.Acknowledgement) error { return nil },
		OnChange:    func() {},
		OnError: func(err error) {
			if err != nil {
				t.Errorf("session manager: %v", err)
			}
		},
		ReconcileConcurrency: 1,
	}))
	if err != nil {
		t.Fatal(err)
	}
	if err := harnesshostlifecycle.PublishDesired(ctx, resources, harnesshostlifecycle.Desired{}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := manager.Close(); err != nil {
			t.Errorf("close session manager: %v", err)
		}
	})
	return ctx, root, resources, store, manager
}

func reserveTestSession(t *testing.T, ctx context.Context, manager *Manager, store *retained.Store, id, cwd, setup string) *Session {
	t.Helper()
	record, err := retained.NewRecord(&sessionpb.SessionSpec{
		Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: cwd, Model: "test-model",
	}, setup, nil)
	if err != nil {
		t.Fatal(err)
	}
	s, err := manager.ReserveFresh(ctx, id, func() error { return store.Write(id, record) })
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func assertSessionBlockers(t *testing.T, s *Session, want []string) {
	t.Helper()
	blockers := s.AutosuspendBlockers()
	if len(blockers) != len(want) {
		t.Fatalf("Session autosuspend blockers = %v, want %v", blockers, want)
	}
	for i, blocker := range blockers {
		if blocker.GetSessionId() != s.ID() || blocker.GetReason() != want[i] {
			t.Fatalf("Session autosuspend blocker %d = %v, want session %q reason %q", i, blocker, s.ID(), want[i])
		}
	}
}

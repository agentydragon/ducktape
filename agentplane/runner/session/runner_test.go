package session

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

func TestSessionEnvironmentCarriesItsOwnDestination(t *testing.T) {
	globalEnvironment := map[string]string{"TZ": "UTC"}
	want := map[string]string{
		"session-1": `{"destination_ref":{"namespace":"test","name":"sandbox","uid":"sandbox-uid"},"session_id":"session-1"}`,
		"session-2": `{"destination_ref":{"namespace":"test","name":"sandbox","uid":"sandbox-uid"},"session_id":"session-2"}`,
	}
	ctx, root, _, store, manager := runnerTestManager(t, globalEnvironment, func(id string) string { return want[id] })
	for id := range want {
		s := runnerTestReserve(t, ctx, manager, store, id, filepath.Join(root, id), "")
		if got := s.Session().Environment["AGENTPLANE_DESTINATION"]; got != want[id] {
			t.Fatalf("%s destination = %q, want %q", id, got, want[id])
		}
	}
	if _, shared := globalEnvironment["AGENTPLANE_DESTINATION"]; shared {
		t.Fatal("a Session's destination leaked into the runner-wide harness environment")
	}
}

func TestSessionBlockersTrackBusyReasonsAndReturnToIdle(t *testing.T) {
	ctx, root, resources, store, manager := runnerTestManager(t, nil, func(string) string { return "test-destination" })
	s := runnerTestReserve(t, ctx, manager, store, "session-1", filepath.Join(root, "project"), "setup script")
	s.SetSetupScript("setup script")
	if err := publish(ctx, resources, s.ID(), Runtime{Phase: Preparing}); err != nil {
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
	if !status.Busy || status.ExecutionState != runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_PREPARING {
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
	if err := publish(ctx, resources, s.ID(), Runtime{Phase: Dormant}); err != nil {
		t.Fatal(err)
	}

	assertSessionBlockers(t, s, nil)
	status, _ = s.Status()
	if status.Busy || status.ExecutionState != runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_DORMANT {
		t.Fatalf("idle session status = %+v, want dormant and not busy", status)
	}
}

func runnerTestManager(t *testing.T, environment map[string]string, destination func(string) string) (context.Context, string, state.State, *retained.Store, *Manager) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	root := t.TempDir()
	store, err := retained.NewStore(filepath.Join(root, "sessions"))
	if err != nil {
		t.Fatal(err)
	}
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
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
	if err := runnerlifecycle.PublishDesired(ctx, resources, runnerlifecycle.Desired{}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := manager.Close(); err != nil {
			t.Errorf("close session manager: %v", err)
		}
	})
	return ctx, root, resources, store, manager
}

func runnerTestReserve(t *testing.T, ctx context.Context, manager *Manager, store *retained.Store, id, cwd, setup string) *Session {
	t.Helper()
	record, err := retained.NewRecord(&runnerpb.SessionSpec{
		Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: cwd, Model: "test-model",
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

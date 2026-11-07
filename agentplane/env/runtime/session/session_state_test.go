package session

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

func TestSessionEnvironmentCarriesItsOwnID(t *testing.T) {
	globalEnvironment := map[string]string{"TZ": "UTC"}
	ctx, root, _, store, manager := newSessionTestManager(t, globalEnvironment)
	for _, id := range []string{"session-1", "session-2"} {
		s := reserveTestSession(t, ctx, manager, store, id, filepath.Join(root, id), "")
		if got := s.Session().Environment["AGENTPLANE_SESSION_ID"]; got != id {
			t.Fatalf("%s session ID environment = %q, want %q", id, got, id)
		}
	}
	if _, shared := globalEnvironment["AGENTPLANE_SESSION_ID"]; shared {
		t.Fatal("a Session's ID leaked into the Host-wide harness environment")
	}
}

func TestSessionBlockersTrackBusyReasonsAndReturnToIdle(t *testing.T) {
	ctx, root, resources, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session-1", filepath.Join(root, "project"), "setup script")
	if err := lifecycle.Publish(ctx, resources, s.ID(), LifecycleStatus{Phase: Preparing}, lifecycle.EnvironmentOwner); err != nil {
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
	if err := lifecycle.Publish(ctx, resources, s.ID(), LifecycleStatus{Phase: Dormant}, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}

	assertSessionBlockers(t, s, nil)
	status, _ = s.Status()
	if status.Busy || status.ExecutionState != sessionpb.SessionExecutionState_SESSION_EXECUTION_STATE_DORMANT {
		t.Fatalf("idle session status = %+v, want dormant and not busy", status)
	}
}

func newSessionTestManager(t *testing.T, environment map[string]string) (context.Context, string, state.State, *retained.Store, *Manager) {
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
		HarnessEnvironment: environment,
		OnChange:           func() {},
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
	if err := lifecycle.PublishEnvironmentDesired(ctx, resources, lifecycle.EnvironmentDesired{Mode: lifecycle.Running}); err != nil {
		t.Fatal(err)
	}
	startSessionOwner(t, ctx, resources, manager)
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
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}}}, Cwd: cwd, Model: "test-model",
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

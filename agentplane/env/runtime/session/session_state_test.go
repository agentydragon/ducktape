package session

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/codexappserver"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/retained"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
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
	host := s.Observe().HarnessHostSession()
	if host.GetEnvironmentHosted().GetHarness().GetState() != harnesshostpb.HarnessAttachment_STATE_STARTING || host.GetPendingCommands() != 1 || host.GetActiveTurnId() != "turn-1" {
		t.Fatalf("busy session host status = %+v, want starting, one pending command, and active turn", host)
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
	host = s.Observe().HarnessHostSession()
	if host.GetEnvironmentHosted().GetHarness().GetState() != harnesshostpb.HarnessAttachment_STATE_DETACHED || host.GetPendingCommands() != 0 || host.GetActiveTurnId() != "" {
		t.Fatalf("idle session host status = %+v, want detached and idle", host)
	}
}

// newTestStore opens the retained Session store under root, backed by resources.
func newTestStore(t *testing.T, root string, resources state.State) *retained.Store {
	t.Helper()
	store, err := retained.NewStore(filepath.Join(root, "sessions"), resources)
	if err != nil {
		t.Fatal(err)
	}
	return store
}

// newTestRecord returns the retained record of a Session hosted by the test Sandbox.
func newTestRecord(t *testing.T, kind sessionpb.Harness, cwd, setup string) *retained.Record {
	t.Helper()
	record, err := retained.NewRecord(&sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: kind}}}, Cwd: cwd, Model: "test-model",
	}, setup, nil)
	if err != nil {
		t.Fatal(err)
	}
	return record
}

// writeTestRecord stores a new test Session's record under id and returns it.
func writeTestRecord(t *testing.T, store *retained.Store, id string, kind sessionpb.Harness, cwd, setup string) *retained.Record {
	t.Helper()
	record := newTestRecord(t, kind, cwd, setup)
	if err := store.Write(id, record); err != nil {
		t.Fatal(err)
	}
	return record
}

func newSessionTestManager(t *testing.T, environment map[string]string) (context.Context, string, state.State, *retained.Store, *Manager) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	root := t.TempDir()
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	store := newTestStore(t, root, resources)
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
	return reserveTestSessionWithHarness(t, ctx, manager, store, id, cwd, setup, sessionpb.Harness_HARNESS_CLAUDE)
}

func reserveTestSessionWithHarness(t *testing.T, ctx context.Context, manager *Manager, store *retained.Store, id, cwd, setup string, kind sessionpb.Harness) *Session {
	t.Helper()
	record := newTestRecord(t, kind, cwd, setup)
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

// Seed the same per-Session hold that a shared app-server installs: a live
// process and a Ready lifecycle for one Session, alongside a dormant reservation.
func TestHarnessHostSharedProcessDoesNotAttachDormantSession(t *testing.T) {
	s, _, manager := frameTestSessionWithHarness(t, []string{"/bin/cat"}, false, nil, sessionpb.Harness_HARNESS_CODEX)
	s.mu.Lock()
	s.codexAppServer = &codexappserver.Process{}
	s.mu.Unlock()
	setLifecycleStatusReady(t, nil, s)
	dormant := reserveTestSessionWithHarness(t, t.Context(), manager, manager.store, "dormant", filepath.Join(t.TempDir(), "project"), "", sessionpb.Harness_HARNESS_CODEX)
	attached := s.Observe().HarnessHostSession()
	if attached.GetEnvironmentHosted().GetHarness().GetState() != harnesshostpb.HarnessAttachment_STATE_ATTACHED {
		t.Fatalf("shared-process Session = %v, want attached", attached)
	}
	idle := dormant.Observe().HarnessHostSession()
	if idle.GetEnvironmentHosted().GetHarness().GetState() != harnesshostpb.HarnessAttachment_STATE_DETACHED || idle.PendingCommands != 0 || idle.ActiveTurnId != "" {
		t.Fatalf("dormant Session beside shared process = %v", idle)
	}

	for _, id := range []string{"queued-1", "queued-2"} {
		if _, err := s.Admit(t.Context(), &commandpb.Command{CommandId: id, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}}, false); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.Emit(t.Context(), observation(&eventpb.TurnStarted{TurnId: "running"}), nil); err != nil {
		t.Fatal(err)
	}
	busy := s.Observe().HarnessHostSession()
	if busy.ActiveTurnId != "running" || busy.PendingCommands != 2 {
		t.Fatalf("busy Session = %v", busy)
	}
	if err := s.NoopCommand(t.Context(), "queued-1", "settled", nil); err != nil {
		t.Fatal(err)
	}
	if got := s.Observe().HarnessHostSession().PendingCommands; got != 1 {
		t.Fatalf("pending after one settlement = %d, want 1", got)
	}
	if err := s.NoopCommand(t.Context(), "queued-2", "settled", nil); err != nil {
		t.Fatal(err)
	}
	if err := s.Emit(t.Context(), observation(&eventpb.TurnCompleted{TurnId: "running", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}), nil); err != nil {
		t.Fatal(err)
	}
	idle = s.Observe().HarnessHostSession()
	if idle.ActiveTurnId != "" || idle.PendingCommands != 0 {
		t.Fatalf("settled Session = %v", idle)
	}
}

func TestHarnessHostSetupAndAttachmentFailure(t *testing.T) {
	ctx, root, resources, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "setup", filepath.Join(root, "project"), "script")
	if got := s.Observe().HarnessHostSession().GetEnvironmentHosted().GetSetup(); got != sessionpb.SetupState_SETUP_STATE_RUNNING {
		t.Fatalf("setup = %v, want running", got)
	}
	if err := s.Emit(ctx, observation(&eventpb.SetupFinished{ExitCode: 1}), nil); err != nil {
		t.Fatal(err)
	}
	hosted := s.Observe().HarnessHostSession().GetEnvironmentHosted()
	if hosted.Setup != sessionpb.SetupState_SETUP_STATE_FAILED || hosted.Harness.FailureCursor != nil {
		t.Fatalf("setup failure became attachment failure: %v", hosted)
	}
	if err := lifecycle.Publish(ctx, resources, s.ID(), LifecycleStatus{Phase: Preparing}, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	if got := s.Observe().HarnessHostSession().GetEnvironmentHosted().Harness.State; got != harnesshostpb.HarnessAttachment_STATE_STARTING {
		t.Fatalf("preparing attachment = %v", got)
	}
	if err := s.Emit(ctx, observation(&eventpb.HarnessLaunchFailed{Reason: "launch failed"}), nil); err != nil {
		t.Fatal(err)
	}
	failure := s.journal.LastCursor()
	if err := lifecycle.Publish(ctx, resources, s.ID(), LifecycleStatus{Phase: Dormant}, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	hosted = s.Observe().HarnessHostSession().GetEnvironmentHosted()
	if hosted.Harness.State != harnesshostpb.HarnessAttachment_STATE_DETACHED || hosted.Harness.GetFailureCursor() != failure {
		t.Fatalf("failed launch attachment = %v", hosted)
	}
	// Recovery must include failures after the initial launch prefix, and a later
	// HarnessStarted must clear them rather than resurrecting an earlier failure.
	for _, event := range []*eventpb.Event{
		observation(&eventpb.HarnessStarted{}),
		observation(&eventpb.HarnessExited{ExitCode: 17}),
		observation(&eventpb.HarnessStarted{}),
	} {
		if err := s.Emit(ctx, event, nil); err != nil {
			t.Fatal(err)
		}
		reloaded := newSession(s.deps, s.id, s.record, s.journal)
		if err := reloaded.load(ctx); err != nil {
			t.Fatal(err)
		}
		attachment := reloaded.Observe().HarnessHostSession().GetEnvironmentHosted().Harness
		if event.GetHarnessExited() != nil {
			if attachment.GetFailureCursor() != s.journal.LastCursor() {
				t.Fatalf("recovered exit failure = %v", attachment)
			}
		} else if attachment.FailureCursor != nil {
			t.Fatalf("start did not clear failure: %v", attachment)
		}
	}
}

package runner

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"
	"testing/synctest"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const lifecycleTestTimeout = 10 * time.Second

func lifecycleTestSession(t *testing.T) (*Runner, *Session) {
	t.Helper()
	workspace := t.TempDir()
	proxy := modelConfigServer(t)
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	if _, err := r.OpenSession(context.Background(), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	return r, r.sessions["session-1"]
}

func waitLifecycle(t *testing.T, r *Runner, predicate func() bool, want string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	for {
		changed := r.changesSince()
		if predicate() {
			return
		}
		select {
		case <-changed:
		case <-ctx.Done():
			t.Fatalf("timed out waiting for %s: goal=%+v runtime=%+v", want, r.lifecycle.goal(), r.lifecycle.status())
		}
	}
}

func waitClose(t *testing.T, done <-chan error) error {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		t.Fatal("runner Close did not finish")
		return ctx.Err()
	}
}

func setRuntimeReady(t *testing.T, s *Session) {
	t.Helper()
	setRuntimePhase(t, s, executionReady)
	s.mu.Lock()
	s.processState = runnerpb.ProcessState_PROCESS_STATE_RUNNING
	s.mu.Unlock()
}

func setRuntimePhase(t *testing.T, s *Session, phase executionPhase) {
	t.Helper()
	if err := setSessionRuntimePhaseForTest(s.runner, s, phase); err != nil {
		t.Fatal(err)
	}
}

func setSessionRuntimePhaseForTest(r *Runner, s *Session, phase executionPhase) error {
	ctx, cancel := context.WithTimeout(r.ctx, lifecycleTestTimeout)
	defer cancel()
	return modifyLifecycleForTest(ctx, r, s.id, sessionControllerName, func(current *sessionRuntimeSpec) {
		current.Phase = phase
		s.mu.Lock()
		current.ProcessID = s.processID
		s.mu.Unlock()
	})
}

// The controller creates its output; COSI watches creation and retries version conflicts.
func modifyLifecycleForTest[T lifecycleSpec[T]](ctx context.Context, r *Runner, id, owner string, modify func(*T)) error {
	var zero T
	ptr := lifecycleResourceFor(id, zero).Metadata()
	if _, err := safe.StateWatchFor[*typed.Resource[T, lifecycleExtension[T]]](ctx, r.lifecycle.state, ptr,
		state.WithEventTypes(state.Created, state.Updated)); err != nil {
		return err
	}
	_, err := safe.StateUpdateWithConflicts(ctx, r.lifecycle.state, ptr, func(value *typed.Resource[T, lifecycleExtension[T]]) error {
		modify(value.TypedSpec())
		return nil
	}, state.WithUpdateOwner(owner))
	return err
}

func registerTestSession(t *testing.T, s *Session) {
	t.Helper()
	r := s.runner
	r.mu.Lock()
	if old := r.sessions[s.id]; old != nil && old != s {
		r.mu.Unlock()
		t.Fatalf("Session %q is already registered", s.id)
	}
	r.sessions[s.id] = s
	r.mu.Unlock()

	s.mu.Lock()
	input := sessionInputSpec{
		Setup: s.setup, ActiveTurn: s.activeTurn, Pending: len(s.pending),
		ProcessID: s.processID, ProcessRunning: s.process != nil && s.process.Running(),
		Inspections:     len(s.reconciliations),
		InspectionReady: len(s.reconciliations) > 0 && s.reconciliations[0].ready,
	}
	s.mu.Unlock()
	input.Cursor = s.journal.LastCursor()
	if err := publishLifecycle(r.lifecycle, s.id, input); err != nil {
		t.Fatal(err)
	}
}

func TestCloseAfterQuiescePreservesSealAndWaitsForSetup(t *testing.T) {
	r, s := lifecycleTestSession(t)
	setupDone := make(chan struct{})
	s.mu.Lock()
	s.setup = runnerpb.SetupState_SETUP_STATE_RUNNING
	s.mu.Unlock()
	s.startOperation(&s.setupJob, func(ctx context.Context) error {
		<-setupDone
		return s.Emit(ctx, observation(&eventpb.SetupFinished{ExitCode: 0}), nil)
	})
	s.wake()
	t.Cleanup(func() {
		select {
		case <-setupDone:
		default:
			close(setupDone)
		}
	})

	if _, err := r.Quiesce(context.Background(), &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatalf("accept quiesce: %v", err)
	}
	waitLifecycle(t, r, func() bool {
		return r.lifecycle.status().Stage == stageHalting && s.runtimeState().Phase == executionStopping
	}, "quiesce halt to wait at setup barrier")

	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close() }()
	waitLifecycle(t, r, func() bool {
		goal := r.lifecycle.goal()
		return goal.Mode == runnerClosing && goal.SealUID == "test-incarnation" &&
			r.lifecycle.status().Stage == stageHalting && s.runtimeState().Phase == executionStopping
	}, "Close to preserve the accepted seal while setup is running")
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned before setup completed: %v", err)
	default:
	}
	if seal := s.journal.SealState(); seal != nil {
		t.Fatalf("journal sealed before setup completed: %+v", seal)
	}

	close(setupDone)
	if err := waitClose(t, closeDone); err != nil {
		t.Fatalf("Close after setup completion: %v", err)
	}
	seal := s.journal.SealState()
	if seal == nil || seal.IncarnationUID != "test-incarnation" {
		t.Fatalf("journal seal = %+v, want accepted incarnation", seal)
	}
	ack, err := r.store.ReadAcknowledgement(s.id)
	if err != nil || ack != nil {
		t.Fatalf("Close unexpectedly acknowledged ingress: ack=%+v err=%v", ack, err)
	}
}

func TestQuiesceWaitsForExactSealedCursorAcknowledgement(t *testing.T) {
	r, s := lifecycleTestSession(t)
	if err := s.Emit(context.Background(), observation(&eventpb.HarnessStderr{Text: "kept"}), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Quiesce(context.Background(), &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatalf("accept quiesce: %v", err)
	}
	waitLifecycle(t, r, func() bool { return r.lifecycle.status().Stage == stageFlushing }, "quiesce to flush sealed journal")
	seal := s.journal.SealState()
	if seal == nil || seal.Cursor == 0 {
		t.Fatalf("journal seal = %+v, want a nonzero cursor", seal)
	}
	if err := r.lifecycle.publishAck(s.id, IngressAcknowledgement{Cursor: seal.Cursor - 1, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.QuiesceStage != controlpb.RunnerFacts_QUIESCE_STAGE_FLUSHING {
		t.Fatalf("facts after under-cursor acknowledgement = %s, want FLUSHING", facts.QuiesceStage)
	}
	if current := r.lifecycle.status(); current.Stage != stageFlushing {
		t.Fatalf("runtime after under-cursor acknowledgement = %v, want FLUSHING", current.Stage)
	}

	if err := r.lifecycle.publishAck(s.id, IngressAcknowledgement{Cursor: seal.Cursor, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool { return r.lifecycle.status().Stage == stageQuiesced }, "exact sealed cursor acknowledgement")
	facts, err = r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.QuiesceStage != controlpb.RunnerFacts_QUIESCE_STAGE_QUIESCED {
		t.Fatalf("facts after exact sealed cursor acknowledgement = %s, want QUIESCED", facts.QuiesceStage)
	}
}

func TestPreparationCompletingAfterCloseCannotMakeSessionReady(t *testing.T) {
	r, s := lifecycleTestSession(t)
	setRuntimePhase(t, s, executionPreparing)
	started, release, finished := make(chan struct{}), make(chan struct{}), make(chan struct{})
	s.startOperation(&s.preparation, func(ctx context.Context) error {
		close(started)
		<-release
		defer close(finished)
		if ctx.Err() == nil {
			return fmt.Errorf("preparation was not canceled during shutdown")
		}
		return nil
	})
	select {
	case <-started:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("preparation did not start")
	}
	t.Cleanup(func() {
		select {
		case <-release:
		default:
			close(release)
		}
	})

	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close() }()
	waitLifecycle(t, r, func() bool {
		return r.lifecycle.goal().Mode == runnerClosing && r.lifecycle.status().Stage == stageHalting && s.runtimeState().Phase == executionStopping
	}, "Close to cancel and join preparation")
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned before preparation completed: %v", err)
	default:
	}
	close(release)
	if err := waitClose(t, closeDone); err != nil {
		t.Fatalf("Close after preparation completed: %v", err)
	}
	select {
	case <-finished:
	default:
		t.Fatal("Close returned without joining the preparation operation")
	}
	finalRuntime, found, err := lifecycleRead[sessionRuntimeSpec](context.Background(), r.lifecycle.state, s.id)
	if err != nil || !found || finalRuntime.Phase != executionHalted {
		t.Fatalf("Session runtime after late preparation completion = %+v, found=%t err=%v; want halted", finalRuntime, found, err)
	}
	s.mu.Lock()
	preparation := s.preparation
	s.mu.Unlock()
	if preparation != nil {
		t.Fatalf("preparation operation remained owned after Close: %+v", preparation)
	}
}

func TestPreparationJoinsExitedProcessReadersBeforeReplacingAttachment(t *testing.T) {
	s, previous := frameTestSession(t, []string{"/bin/true"})
	if _, err := previous.Wait(context.Background()); err != nil {
		t.Fatal(err)
	}
	synctest.Test(t, func(t *testing.T) {
		// The OS process has exited, but its stdout callback still owns the
		// attachment until its final journal writes and exit handling complete.
		s.readers.Add(1)
		returned := make(chan struct{})
		var prepareErr error
		go func() { prepareErr = s.ensureReady(context.Background()); close(returned) }()
		synctest.Wait()
		select {
		case <-returned:
			t.Error("preparation reached successor startup before the old reader completed")
		default:
		}
		s.readers.Done()
		synctest.Wait()
		<-returned
		if prepareErr == nil {
			t.Fatal("expected the fixture's unconfigured harness to reject successor startup")
		}
	})
}

func TestNewIncarnationWaitsForTeardownBeforeResettingDemand(t *testing.T) {
	r, s := lifecycleTestSession(t)
	// These hosts are unconfigured, so old demand can be seeded without a
	// native effect. Adoption must discard it even when the host is dormant.
	l := r.lifecycle
	l.publishMu.Lock()
	err := publishLifecycle(l, "codex", codexInputSpec{Demand: 3})
	if err == nil {
		err = publishLifecycle(l, "executor", executorInputSpec{Demand: 4})
	}
	l.publishMu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	setupDone := make(chan struct{})
	t.Cleanup(func() {
		select {
		case <-setupDone:
		default:
			close(setupDone)
		}
	})
	s.mu.Lock()
	s.setup = runnerpb.SetupState_SETUP_STATE_RUNNING
	s.mu.Unlock()
	s.startOperation(&s.setupJob, func(ctx context.Context) error {
		<-setupDone
		return s.Emit(ctx, observation(&eventpb.SetupFinished{ExitCode: 0}), nil)
	})
	s.wake()
	if _, err := r.Quiesce(context.Background(), &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool {
		return r.lifecycle.status().Stage == stageHalting && s.runtimeState().Phase == executionStopping
	}, "halt waiting for setup")
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	adopted := make(chan error, 1)
	go func() { adopted <- r.adoptIncarnation(ctx, "replacement-incarnation") }()
	select {
	case err := <-adopted:
		t.Fatalf("adoption overtook accepted setup: %v", err)
	default:
	}
	if r.lifecycle.goal().Mode != runnerQuiescing {
		t.Fatal("new incarnation reopened execution during teardown")
	}
	close(setupDone)
	select {
	case err := <-adopted:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("new incarnation waited for ingress acknowledgement")
	}
	if seal := s.journal.SealState(); seal != nil {
		t.Fatalf("new incarnation did not reopen old seal: %+v", seal)
	}
	if !r.lifecycle.accepting() {
		t.Fatal("new incarnation did not reopen admission")
	}
	for kind, id := range map[string]string{codexInputType: "codex", executorInputType: "executor"} {
		switch kind {
		case codexInputType:
			input, _, err := lifecycleRead[codexInputSpec](r.ctx, r.lifecycle.state, id)
			if err != nil || input.Demand != 0 {
				t.Fatalf("Codex old demand survived adoption: %+v, %v", input, err)
			}
		case executorInputType:
			input, _, err := lifecycleRead[executorInputSpec](r.ctx, r.lifecycle.state, id)
			if err != nil || input.Demand != 0 {
				t.Fatalf("executor old demand survived adoption: %+v, %v", input, err)
			}
		}
	}
}

func TestIdleProcessExitConsumesCompletedCommandDemand(t *testing.T) {
	s, p := frameTestSession(t, []string{"/bin/sh", "-c", "cat >/dev/null"})
	s.mu.Lock()
	s.processID = "completed-command-process"
	s.mu.Unlock()
	registerTestSession(t, s)
	waitLifecycle(t, s.runner, func() bool {
		_, found, _ := lifecycleRead[sessionRuntimeSpec](s.runner.ctx, s.runner.lifecycle.state, s.id)
		return found
	}, "initial Session runtime")
	setRuntimeReady(t, s)
	// Ordinary commands submitted to an already-ready process increment Demand
	// without another preparation attempt. Both have completed before this exit.
	s.requestExecution()
	s.requestExecution()
	if err := p.Stop(context.Background(), time.Second); err != nil {
		t.Fatal(err)
	}
	s.wake()
	waitLifecycle(t, s.runner, func() bool {
		current := s.runtimeState()
		return current.Phase == executionDormant && current.Attempt == 2
	}, "idle process exit to consume completed demand")
	s.mu.Lock()
	preparation := s.preparation
	s.mu.Unlock()
	if preparation != nil {
		t.Fatal("idle process exit started a successor without new work")
	}
}

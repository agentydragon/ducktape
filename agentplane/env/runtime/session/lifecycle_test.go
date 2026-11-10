package session

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/oneoff"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const lifecycleTestTimeout = 10 * time.Second

type preparationDuringQuiesceAdapter struct {
	harness.Adapter
	started chan struct{}
	release chan struct{}
	session *Session
}

func (a preparationDuringQuiesceAdapter) Handshake(context.Context) (string, error) {
	close(a.started)
	<-a.release
	return "native-session", nil
}

func (a preparationDuringQuiesceAdapter) Recover(ctx context.Context) error {
	close(a.started)
	<-a.release
	return a.session.Emit(ctx, observation(&eventpb.HarnessStderr{Text: "replayed response"}), nil)
}

func waitSession(t *testing.T, s *Session, predicate func(LifecycleStatus) bool, label string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	_, err := safe.StateWatchFor[*typed.Resource[LifecycleStatus, lifecycle.Extension[LifecycleStatus]]](ctx, s.deps.state, lifecycle.ResourceFor(s.id, LifecycleStatus{}).Metadata(), state.WithEventTypes(state.Created, state.Updated), state.WithCondition(func(value resource.Resource) (bool, error) {
		current, ok := value.(*typed.Resource[LifecycleStatus, lifecycle.Extension[LifecycleStatus]])
		if !ok {
			return false, fmt.Errorf("unexpected Session lifecycle status %T", value)
		}
		return predicate(*current.TypedSpec()), nil
	}))
	if err != nil {
		t.Fatalf("wait for %s: %v", label, err)
	}
}

func withSessionControllerGate(t *testing.T, s *Session, gate *sync.Mutex, setup func()) {
	t.Helper()
	waitSession(t, s, func(LifecycleStatus) bool { return true }, "initial Session lifecycle status")
	gate.Lock()
	defer gate.Unlock()
	setup()
}

type heldInspectionAdapter struct {
	harness.Adapter
	started  chan struct{}
	release  chan struct{}
	returned chan struct{}
}

func (*heldInspectionAdapter) BlocksCommandDispatch() bool { return true }

func (a *heldInspectionAdapter) Reconcile(_ context.Context, turn string, _ bool) (*eventpb.ConversationReconciled, error) {
	close(a.started)
	<-a.release
	defer close(a.returned)
	return &eventpb.ConversationReconciled{TurnId: turn}, nil
}

func TestSessionPreFenceWaitsForInspectionJournalCommit(t *testing.T) {
	var gate sync.Mutex
	s, _, manager := frameTestSessionWithControllerGate(t, []string{"/bin/cat"}, true, &gate)
	inspection := &heldInspectionAdapter{started: make(chan struct{}), release: make(chan struct{}), returned: make(chan struct{})}
	withSessionControllerGate(t, s, &gate, func() {
		setLifecycleStatusReady(t, manager, s)
		s.mu.Lock()
		s.adapter = inspection
		s.reconciliations = append(s.reconciliations, &pendingReconciliation{turnID: "late-turn", adapter: inspection, ready: true})
		s.mu.Unlock()
		s.wake()
	})
	select {
	case <-inspection.started:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("inspection operation did not start")
	}
	if err := lifecycle.PublishEnvironmentDesired(t.Context(), manager.state, lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, Generation: 1}); err != nil {
		t.Fatal(err)
	}
	waitSession(t, s, func(current LifecycleStatus) bool {
		return current.EnvironmentGeneration == 1 && current.Phase == Halted && current.ProcessStopped && !current.PreFenceSettled
	}, "native process stopped while inspection is active")
	held, releaseBatch := make(chan struct{}), make(chan struct{})
	batchDone := make(chan error, 1)
	go func() {
		batchDone <- s.journal.WithBatch(t.Context(), func(context.Context) error {
			close(held)
			<-releaseBatch
			return nil
		})
	}()
	<-held
	close(inspection.release)
	<-inspection.returned
	current, found, err := ReadLifecycleStatus(t.Context(), manager.state, s.id)
	if err != nil || !found || current.PreFenceSettled {
		t.Fatalf("pre-fence settled before inspection observation committed: status=%+v found=%t err=%v", current, found, err)
	}
	close(releaseBatch)
	if err := <-batchDone; err != nil {
		t.Fatal(err)
	}
	waitSession(t, s, func(current LifecycleStatus) bool {
		return current.EnvironmentGeneration == 1 && current.PreFenceSettled
	}, "inspection observation committed and pre-fence cleanup settled")
}

// setSessionPhase directly changes controller-owned lifecycle status; callers must
// pause the Session controller for the fixture mutation.
func setSessionPhase(t *testing.T, s *Session, phase Phase) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	ptr := lifecycle.ResourceFor(s.id, LifecycleStatus{}).Metadata()
	_, err := safe.StateWatchFor[*typed.Resource[LifecycleStatus, lifecycle.Extension[LifecycleStatus]]](ctx, s.deps.state, ptr, state.WithEventTypes(state.Created, state.Updated))
	if err != nil {
		t.Fatal(err)
	}
	_, err = safe.StateUpdateWithConflicts(ctx, s.deps.state, ptr, func(value *typed.Resource[LifecycleStatus, lifecycle.Extension[LifecycleStatus]]) error {
		value.TypedSpec().Phase = phase
		value.TypedSpec().ProcessID = s.processID
		return nil
	}, state.WithUpdateOwner(ControllerName))
	if err != nil {
		t.Fatal(err)
	}
}

func TestQuiesceDuringPreparationKeepsJournalWritable(t *testing.T) {
	for _, phase := range []string{"handshake", "recovery"} {
		t.Run(phase, func(t *testing.T) {
			var gate sync.Mutex
			s, p, m := frameTestSessionWithControllerGate(t, []string{"/bin/cat"}, true, &gate)
			adapter := preparationDuringQuiesceAdapter{started: make(chan struct{}), release: make(chan struct{}), session: s}
			t.Cleanup(func() {
				select {
				case <-adapter.release:
				default:
					close(adapter.release)
				}
			})
			preparing := make(chan context.Context, 1)
			ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
			defer cancel()
			var op *oneoff.Operation
			withSessionControllerGate(t, s, &gate, func() {
				setSessionPhase(t, s, Preparing)
				s.connection.readers.Add(2)
				go s.connection.readStdout(p, func(ctx context.Context, line []byte) error { return s.receive(ctx, line, adapter) })
				go s.connection.readStderr(p)
				s.startOperation(&s.preparation, func(ctx context.Context) error {
					preparing <- ctx
					if phase == "recovery" {
						return s.recoverAdapter(ctx, adapter)
					}
					return s.prepare(ctx, adapter, p.PID())
				})
				s.mu.Lock()
				op = s.preparation
				s.mu.Unlock()
				select {
				case <-adapter.started:
				case <-ctx.Done():
					t.Fatal("preparation did not start")
				}
				if _, err := s.Admit(ctx, &commandpb.Command{CommandId: "test-input", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "test input"}}}, false); err != nil {
					t.Fatal(err)
				}
				if err := lifecycle.PublishEnvironmentDesired(ctx, m.state, lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, Generation: 1}); err != nil {
					t.Fatal(err)
				}
			})
			preparationCtx := <-preparing
			select {
			case <-preparationCtx.Done():
			case <-ctx.Done():
				t.Fatal("quiesce did not cancel preparation")
			}
			close(adapter.release)
			select {
			case <-op.Done():
			case <-ctx.Done():
				t.Fatal("quiesce did not join preparation")
			}
			if _, err := s.journal.GetCommand(ctx, "test-input"); err != nil {
				t.Fatalf("quiesce poisoned admitted journal: %v", err)
			}
			waitSession(t, s, func(current LifecycleStatus) bool { return current.Phase == Halted }, "halted Session")

			if err := s.stopProcess(ctx); err != nil {
				t.Fatal(err)
			}
			if err := s.sealJournal(ctx); err != nil {
				t.Fatal(err)
			}
			command, err := s.journal.GetCommand(ctx, "test-input")
			if err != nil || command == nil || command.TerminalCursor == nil {
				t.Fatalf("undispatched admission was not settled: command=%+v err=%v", command, err)
			}
		})
	}
}

func TestPreparationCompletingAfterShutdownCannotMakeSessionReady(t *testing.T) {
	var gate sync.Mutex
	s, _, m := frameTestSessionWithControllerGate(t, []string{"/bin/cat"}, true, &gate)
	started, release, finished := make(chan struct{}), make(chan struct{}), make(chan struct{})
	t.Cleanup(func() {
		select {
		case <-release:
		default:
			close(release)
		}
	})
	withSessionControllerGate(t, s, &gate, func() {
		setSessionPhase(t, s, Preparing)
		s.startOperation(&s.preparation, func(ctx context.Context) error {
			close(started)
			<-release
			defer close(finished)
			if ctx.Err() == nil {
				return fmt.Errorf("preparation was not canceled")
			}
			return nil
		})
		<-started
		if err := lifecycle.PublishEnvironmentDesired(context.Background(), m.state, lifecycle.EnvironmentDesired{Mode: lifecycle.Closing, Generation: 1}); err != nil {
			t.Fatal(err)
		}
	})
	waitSession(t, s, func(current LifecycleStatus) bool { return current.Phase == Stopping }, "shutdown to stop Session")
	select {
	case <-finished:
		t.Fatal("preparation finished before test released it")
	default:
	}
	close(release)
	waitSession(t, s, func(current LifecycleStatus) bool { return current.Phase == Halted }, "late preparation to settle")
	select {
	case <-finished:
	default:
		t.Fatal("halt did not join preparation")
	}
}

func TestPreparationJoinsExitedProcessReadersBeforeReplacingAttachment(t *testing.T) {
	s, previous, manager := frameTestSession(t, []string{"/bin/true"})
	if _, err := previous.Wait(context.Background()); err != nil {
		t.Fatal(err)
	}
	// Wait for the external controller to finish retiring the status before
	// watching that collection inside a synthetic-time bubble.
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	for {
		_, found, err := supervision.ReadStatus(ctx, manager.state, lifecycle.Namespace, previous.ID())
		if err != nil {
			t.Fatal(err)
		}
		if !found {
			break
		}
		select {
		case <-ctx.Done():
			t.Fatal("process status did not retire")
		case <-time.After(time.Millisecond):
		}
	}
	synctest.Test(t, func(t *testing.T) {
		s.connection.readers.Add(1)
		returned := make(chan struct{})
		var prepareErr error
		go func() { prepareErr = s.ensureReady(context.Background()); close(returned) }()
		synctest.Wait()
		select {
		case <-returned:
			t.Error("preparation reached successor startup before prior reader completed")
		default:
		}
		s.connection.readers.Done()
		synctest.Wait()
		<-returned
		if prepareErr == nil {
			t.Fatal("unconfigured harness unexpectedly started")
		}
	})
}

func TestIdleProcessExitConsumesCompletedCommandDemand(t *testing.T) {
	var gate sync.Mutex
	s, p, manager := frameTestSessionWithControllerGate(t, []string{"/bin/sh", "-c", "cat >/dev/null"}, true, &gate)
	withSessionControllerGate(t, s, &gate, func() { setLifecycleStatusReady(t, manager, s) })
	s.requestExecution()
	s.requestExecution()
	if err := p.Stop(context.Background()); err != nil {
		t.Fatal(err)
	}
	s.wake()
	waitSession(t, s, func(current LifecycleStatus) bool { return current.Phase == Dormant && current.ObservedDemand == 2 }, "completed demand after idle exit")
	s.mu.Lock()
	op := s.preparation
	s.mu.Unlock()
	if op != nil {
		t.Fatal("idle exit started a successor without new work")
	}
}

func TestPreparationCompletionSchedulesSessionController(t *testing.T) {
	for _, staleInput := range []bool{false, true} {
		t.Run(map[bool]string{false: "unchanged-input", true: "stale-input"}[staleInput], func(t *testing.T) {
			var gate sync.Mutex
			s, p, m := frameTestSessionWithControllerGate(t, []string{"/bin/cat"}, true, &gate)
			withSessionControllerGate(t, s, &gate, func() {
				setSessionPhase(t, s, Preparing)
				// Complete without the worker's normal snapshot refresh, so the
				// operation revision alone must schedule the real QController.
				if staleInput {
					m.publishMu.Lock()
					defer m.publishMu.Unlock()
					input, _, err := ReadReconcileSnapshot(m.ctx, m.state, s.id)
					if err == nil {
						input.ProcessID = ""
						input.ProcessRunning = false
						err = lifecycle.Publish(m.ctx, m.state, s.id, input, lifecycle.EnvironmentOwner)
					}
					if err != nil {
						t.Fatal(err)
					}
				}
				op := oneoff.New(m.ctx)
				op.Register(&s.work)
				s.mu.Lock()
				s.preparation = op
				s.mu.Unlock()
				if err := op.Start(
					&s.mu,
					func(bool) error { return m.signalOperation(s.id) },
					m.fail, func(context.Context) error { return nil }, nil, nil,
				); err != nil {
					t.Fatal(err)
				}
				select {
				case <-op.Done():
				case <-time.After(lifecycleTestTimeout):
					t.Fatal("preparation operation did not finish")
				}
			})
			waitSession(t, s, func(current LifecycleStatus) bool { return current.Phase == Ready && current.ProcessID == p.ID() }, "preparation ready after completion signal")
		})
	}
}

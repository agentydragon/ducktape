package session

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/scripts"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/agentydragon/ducktape/agentplane/runner/workspace"
	"github.com/cosi-project/runtime/pkg/controller"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

type frameTestAdapter struct {
	harness.Adapter
	onFrame func(context.Context, json.RawMessage, uint64) error
}

type pendingSubmitAdapter struct {
	harness.Adapter
	submits int
}

func (*pendingSubmitAdapter) BlocksCommandDispatch() bool { return false }

func (a *pendingSubmitAdapter) Submit(context.Context, string, string) error {
	a.submits++
	return &harness.NativeResponsePendingError{Cause: context.DeadlineExceeded}
}

func (a frameTestAdapter) OnFrame(ctx context.Context, raw json.RawMessage, sequence uint64) error {
	return a.onFrame(ctx, raw, sequence)
}

func frameTestSession(t *testing.T, command []string) (*Session, *supervision.ManagedProcess, *Manager) {
	return frameTestSessionWithController(t, command, false)
}

func frameTestSessionWithController(t *testing.T, command []string, controller bool) (*Session, *supervision.ManagedProcess, *Manager) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	root := t.TempDir()
	store, err := retained.NewStore(filepath.Join(root, "sessions"))
	if err != nil {
		t.Fatal(err)
	}
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(resources, zap.L())
	if err != nil {
		t.Fatal(err)
	}
	var workers sync.WaitGroup
	host := supervision.NewHost(ctx, resources, runnerlifecycle.Namespace, &workers, func(err error) {
		if err != nil && !errors.Is(err, context.Canceled) {
			t.Error(err)
		}
	})
	bootstrapHost, err := bootstrap.NewHost(bootstrap.Config{}, bootstrap.Dependencies{Context: ctx, State: resources, Workers: &workers, Admission: func(ctx context.Context, _ runnerlifecycle.Desired, work func(context.Context) error) error {
		return work(ctx)
	}, OnError: func(err error) {
		if err != nil {
			t.Error(err)
		}
	}})
	if err != nil {
		t.Fatal(err)
	}
	launch := func(ctx context.Context, owner string, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
		return host.Launch(ctx, owner, 0, supervision.NewSubprocessSupervisor(nil), processConfig)
	}
	scriptRunner, err := scripts.New(scripts.Config{LaunchProcess: launch})
	if err != nil {
		t.Fatal(err)
	}
	m, err := NewManager(Config{Context: ctx, State: resources, Store: store, WorkspaceRoot: filepath.Dir(root), Scripts: scriptRunner, BootstrapHost: bootstrapHost, EnsureWorkspace: func(_ context.Context, cwd string) (string, error) { return workspace.Resolve(cwd, filepath.Dir(root)) }, LaunchProcess: launch, ResolveModelConfig: func(context.Context, runnerpb.Harness, string) (*int, error) { return nil, nil }, Destination: func(string) string { return "test-destination" }, PublishSeal: func(string, *journal.SealState) error { return nil }, PublishAck: func(string, retained.Acknowledgement) error { return nil }, OnChange: func() {}, OnError: func(err error) {
		if err != nil && !errors.Is(err, context.Canceled) {
			t.Error(err)
		}
	}, ReconcileConcurrency: 1})
	if err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(host.Controller()); err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterController(&testAggregateController{}); err != nil {
		t.Fatal(err)
	}
	if controller {
		if err := engine.RegisterQController(m.Controller()); err != nil {
			t.Fatal(err)
		}
	}
	if err := runnerlifecycle.PublishDesired(ctx, resources, runnerlifecycle.Desired{}); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := engine.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
			t.Error(err)
		}
	}()
	record, err := retained.NewRecord(&runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: root, Model: "test"}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	s, err := m.ReserveFresh(ctx, "session", func() error { return store.Write("session", record) })
	if err != nil {
		t.Fatal(err)
	}
	p, err := host.Launch(ctx, ControllerName+"/"+s.id, 0, supervision.NewSubprocessSupervisor(nil), supervision.ProcessConfig{Command: command, Dir: root, Env: map[string]string{}})
	if err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	s.process = p
	s.processID = p.ID()
	s.mu.Unlock()
	s.wake()
	t.Cleanup(func() {
		if err := p.Stop(context.Background()); err != nil {
			t.Error(err)
		}
		cancel()
		<-done
		workers.Wait()
		s.work.Wait()
		s.readers.Wait()
		if err := m.Close(); err != nil {
			t.Error(err)
		}
	})
	return s, p, m
}

func TestNativeReplyWaitsForTranslationCommit(t *testing.T) {
	s, p, _ := frameTestSession(t, []string{"/bin/sh", "-c", `while IFS= read -r line; do printf '%s\n' "$line"; done`})
	received, release := make(chan struct{}), make(chan struct{})
	adapter := frameTestAdapter{onFrame: func(ctx context.Context, raw json.RawMessage, sequence uint64) error {
		if s.journal.LastCursor() != 1 {
			t.Errorf("outbound frame was not committed before native delivery: cursor %d", s.journal.LastCursor())
		}
		if err := s.Emit(ctx, observation(&eventpb.HarnessStderr{Text: "translated"}), []uint64{sequence}); err != nil {
			return err
		}
		close(received)
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}}
	s.readers.Add(1)
	go s.readStdout(p, adapter)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan nativeResult, 1)
	go func() {
		receipt, err := s.RequestNativeHandshake(ctx, json.RawMessage(`{"request":"echo"}`), func(raw json.RawMessage) bool { return string(raw) == `{"request":"echo"}` })
		done <- nativeResult{receipt: receipt, err: err}
	}()
	select {
	case <-received:
	case <-ctx.Done():
		t.Fatal("native frame never reached translation")
	}
	select {
	case result := <-done:
		t.Fatalf("reply escaped before translation commit: %v", result)
	default:
	}
	if s.journal.LastCursor() != 1 {
		t.Fatal("uncommitted inbound batch was published")
	}
	close(release)
	select {
	case result := <-done:
		if result.err != nil {
			t.Fatal(result.err)
		}
		if result.receipt.Sequence != 2 {
			t.Fatalf("native sequence = %d", result.receipt.Sequence)
		}
	case <-ctx.Done():
		t.Fatal("committed reply was not delivered")
	}
	entries, err := s.journal.Since(ctx, 0, 128)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 3 || entries[2].GetEvent().GetHarnessStderr().Text != "translated" || entries[2].GetEvent().SourceSequences[0] != 2 {
		t.Fatalf("reply did not carry its committed translation: %v", entries)
	}
}

func TestDispatchKeepsUncertainNativeWritePendingAndDoesNotResend(t *testing.T) {
	s, _, _ := frameTestSession(t, []string{"/bin/cat"})
	ctx := context.Background()
	command := &commandpb.Command{
		CommandId: "input-1",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}},
	}
	if _, _, err := s.journal.Admit(ctx, command, ""); err != nil {
		t.Fatal(err)
	}
	adapter := &pendingSubmitAdapter{}
	s.mu.Lock()
	s.adapter = adapter
	s.mu.Unlock()
	// This primitive test drives dispatch directly; seed its readiness observation.
	setRuntimeReady(t, nil, s)
	if err := s.dispatch(ctx, command); err != nil {
		t.Fatal(err)
	}
	record, err := s.journal.GetCommand(ctx, command.CommandId)
	if err != nil {
		t.Fatal(err)
	}
	if adapter.submits != 1 || record.TerminalCursor != nil {
		t.Fatalf("pending dispatch = submits %d, command %+v", adapter.submits, record)
	}
	s.mu.Lock()
	s.dispatched = make(map[string]bool)
	s.mu.Unlock()
	if err := s.dispatch(ctx, command); err != nil {
		t.Fatal(err)
	}
	if adapter.submits != 1 {
		t.Fatalf("dispatch-planned command resent %d times", adapter.submits)
	}
}

func TestRecoveryKeepsDispatchedInterruptPending(t *testing.T) {
	s, _, _ := frameTestSession(t, []string{"/bin/cat"})
	ctx := context.Background()
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "turn-1"}), nil); err != nil {
		t.Fatal(err)
	}
	command := &commandpb.Command{
		CommandId: "interrupt-1",
		Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "turn-1"}},
	}
	if _, _, err := s.journal.Admit(ctx, command, "turn-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.journal.DispatchPlanned(ctx, command.CommandId); err != nil {
		t.Fatal(err)
	}
	if err := s.recover(ctx); err != nil {
		t.Fatal(err)
	}
	record, err := s.journal.GetCommand(ctx, command.CommandId)
	if err != nil {
		t.Fatal(err)
	}
	if record.TerminalCursor != nil {
		t.Fatalf("dispatched interruption was settled during recovery: %+v", record)
	}
}

func TestNativeHandshakeWaitCancellationLeavesLateResponseJournaled(t *testing.T) {
	script := `while IFS= read -r line; do printf '{"received":true}\n'; IFS= read -r release; [ "$release" = '{"release":true}' ] || exit 1; printf '%s\n' "$line"; done`
	s, p, _ := frameTestSession(t, []string{"/bin/sh", "-c", script})
	received := make(chan struct{})
	lateReplyReceived := make(chan struct{})
	adapter := frameTestAdapter{onFrame: func(ctx context.Context, raw json.RawMessage, sequence uint64) error {
		if string(raw) == `{"received":true}` {
			close(received)
			return nil
		}
		if err := s.Emit(ctx, observation(&eventpb.HarnessStderr{Text: string(raw)}), []uint64{sequence}); err != nil {
			return err
		}
		close(lateReplyReceived)
		return nil
	}}
	s.readers.Add(1)
	go s.readStdout(p, adapter)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := s.RequestNativeHandshake(ctx, json.RawMessage(`{"request":"echo"}`), func(raw json.RawMessage) bool {
			return string(raw) == `{"request":"echo"}`
		})
		done <- err
	}()
	waitCtx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer waitCancel()
	select {
	case <-received:
	case <-waitCtx.Done():
		t.Fatal("native process did not read the handshake request")
	}
	cancel()
	if err := <-done; !errors.Is(err, harness.ErrNativeResponsePending) {
		t.Fatalf("cancelled native wait error = %v", err)
	}
	if err := p.WriteLine(context.Background(), `{"release":true}`); err != nil {
		t.Fatal(err)
	}
	select {
	case <-lateReplyReceived:
	case <-waitCtx.Done():
		t.Fatal("late native response was not delivered to the adapter")
	}
	if err := s.journal.WaitBeyond(waitCtx, 3); err != nil {
		t.Fatalf("late native response was not committed: %v", err)
	}
	entries, err := s.journal.Since(context.Background(), 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 4 || entries[3].GetEvent().GetHarnessStderr() == nil || entries[3].GetEvent().GetHarnessStderr().Text != `{"request":"echo"}` {
		t.Fatalf("late response journal = %v", entries)
	}
}

func TestStdoutRetainsFinalFrameWithoutNewline(t *testing.T) {
	s, p, _ := frameTestSession(t, []string{"/bin/sh", "-c", `printf '{"final":true}\r'`})
	adapter := frameTestAdapter{onFrame: func(context.Context, json.RawMessage, uint64) error { return nil }}
	s.readers.Add(1)
	go s.readStdout(p, adapter)
	s.readers.Wait()
	entries, err := s.journal.Since(context.Background(), 0, 128)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 || entries[0].GetEvent().GetNative().Line != `{"final":true}` || entries[1].GetEvent().GetHarnessExited() == nil {
		t.Fatalf("final native frame was lost or changed: %v", entries)
	}
}

func TestConfirmationReplayKeepsTerminalReceipt(t *testing.T) {
	ctx := context.Background()
	journal := openTestJournal(t)
	store, err := retained.NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	record, err := retained.NewRecord(&runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: t.TempDir(), Model: "test-model"}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	deps := sessionDependencies{ctx: ctx, state: resources, store: store, publish: func(*Session, bool) {}, changed: func() {}, fail: func(err error) { t.Error(err) }}
	s := newSession(deps, "session-1", &record, journal)
	command := &commandpb.Command{CommandId: "input-1", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}}
	if _, _, err := journal.Admit(ctx, command, ""); err != nil {
		t.Fatal(err)
	}
	confirmation := harness.UserMessageConfirmation{HarnessMessageID: "message-1", Text: "hello", OriginCommandIDs: []string{"input-1"}, TurnID: "turn-1"}
	if err := s.ConfirmUserMessage(ctx, confirmation, nil); err != nil {
		t.Fatal(err)
	}
	before := journal.LastCursor()
	if err := s.ConfirmUserMessage(ctx, confirmation, nil); err != nil {
		t.Fatal(err)
	}
	if journal.LastCursor() != before {
		t.Fatal("replay published another terminal event")
	}
}

type rpcReconciliationAdapter struct {
	harness.Adapter
	session *Session
}

func (*rpcReconciliationAdapter) BlocksCommandDispatch() bool { return false }

func (a *rpcReconciliationAdapter) OnFrame(ctx context.Context, raw json.RawMessage, sequence uint64) error {
	if string(raw) == `{"interrupt":true}` {
		return a.session.Emit(ctx, observation(&eventpb.TurnCompleted{TurnId: "turn", Status: eventpb.TurnStatus_TURN_STATUS_INTERRUPTED}), []uint64{sequence})
	}
	return nil
}

func (a *rpcReconciliationAdapter) Reconcile(ctx context.Context, turn string, _ bool) (*eventpb.ConversationReconciled, error) {
	// Even a concurrent lifecycle wake cannot dispatch the next input while reconciliation is waiting.
	if err := a.session.dispatch(ctx, &commandpb.Command{CommandId: "later"}); err != nil {
		return nil, err
	}
	a.session.mu.Lock()
	dispatched := a.session.dispatched["later"]
	a.session.mu.Unlock()
	if dispatched {
		return nil, errors.New("input dispatched before reconciliation")
	}
	_, err := a.session.RequestNativeHandshake(ctx, json.RawMessage(`{"history":true}`), func(raw json.RawMessage) bool { return string(raw) == `{"history":true}` })
	return &eventpb.ConversationReconciled{TurnId: turn}, err
}

func TestInterruptedReconciliationLeavesStdoutFreeForHistoryRPC(t *testing.T) {
	s, p, m := frameTestSessionWithController(t, []string{"/bin/sh", "-c", `while IFS= read -r line; do printf '%s\n' "$line"; done`}, true)
	adapter := &rpcReconciliationAdapter{session: s}
	s.mu.Lock()
	s.adapter = adapter
	s.mu.Unlock()
	setRuntimeReady(t, m, s)
	if err := s.Emit(context.Background(), observation(&eventpb.TurnStarted{TurnId: "turn"}), nil); err != nil {
		t.Fatal(err)
	}
	s.readers.Add(1)
	go s.readStdout(p, adapter)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := s.SendNative(ctx, json.RawMessage(`{"interrupt":true}`)); err != nil {
		t.Fatal(err)
	}
	var cursor uint64
	for {
		entries, err := s.journal.Since(ctx, cursor, 100)
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range entries {
			cursor = entry.Cursor
			if entry.Event.GetConversationReconciled() != nil {
				return
			}
		}
		if err := s.journal.WaitBeyond(ctx, cursor); err != nil {
			t.Fatal(err)
		}
	}
}

func openTestJournal(t *testing.T) *journal.Journal {
	t.Helper()
	journal, err := journal.Open(context.Background(), filepath.Join(t.TempDir(), "journal.sqlite"), "test-source")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := journal.Close(); err != nil {
			t.Error(err)
		}
	})
	return journal
}

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && os.Args[1] == supervision.SupervisorMode {
		os.Exit(supervision.SupervisorMain(os.Args[2:]))
	}
	os.Exit(m.Run())
}

func setRuntimeReady(t *testing.T, m *Manager, s *Session) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ptr := componentResource(s.id, Runtime{}).Metadata()
	if m == nil {
		if err := s.deps.state.Create(ctx, componentResource(s.id, Runtime{Phase: Ready, ProcessID: s.processID}), state.WithCreateOwner(ControllerName)); err != nil {
			t.Fatal(err)
		}
		s.mu.Lock()
		s.processState = runnerpb.ProcessState_PROCESS_STATE_RUNNING
		s.mu.Unlock()
		return
	}
	_, err := safe.StateWatchFor[*typed.Resource[Runtime, componentExtension[Runtime]]](ctx, s.deps.state, ptr, state.WithEventTypes(state.Created, state.Updated))
	if err != nil {
		t.Fatal(err)
	}
	_, err = safe.StateUpdateWithConflicts(ctx, s.deps.state, ptr, func(value *typed.Resource[Runtime, componentExtension[Runtime]]) error {
		value.TypedSpec().Phase = Ready
		value.TypedSpec().ProcessID = s.processID
		return nil
	}, state.WithUpdateOwner(ControllerName))
	if err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	s.processState = runnerpb.ProcessState_PROCESS_STATE_RUNNING
	s.mu.Unlock()
}

// The test aggregate publishes only the Runner-wide gate needed by Session.
type testAggregateController struct{}

func (*testAggregateController) Name() string { return "test-aggregate" }
func (*testAggregateController) Inputs() []controller.Input {
	return []controller.Input{{Namespace: runnerlifecycle.Namespace, Type: runnerlifecycle.DesiredStateType, Kind: controller.InputWeak}}
}

func (*testAggregateController) Outputs() []controller.Output {
	return []controller.Output{{Type: runnerlifecycle.AggregateStatusType, Kind: controller.OutputExclusive}}
}

func (*testAggregateController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}
		desired, found, err := runnerlifecycle.ReadDesired(ctx, r)
		if err != nil {
			return err
		}
		if !found {
			continue
		}
		stage := runnerlifecycle.StageRunning
		if desired.Mode != runnerlifecycle.Running {
			stage = runnerlifecycle.Halting
		}
		if _, err := runnerlifecycle.WriteStatus(ctx, r, runnerlifecycle.Status{Generation: desired.Generation, Stage: stage}); err != nil {
			return err
		}
		r.ResetRestartBackoff()
	}
}

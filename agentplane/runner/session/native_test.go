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
	codexadapter "github.com/agentydragon/ducktape/agentplane/runner/codex"
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
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

type frameTestAdapter struct {
	harness.Adapter
	onFrame func(context.Context, json.RawMessage, uint64) error
}

func TestStaleNativeCodexCompletionRetainsFrameAndSuccessor(t *testing.T) {
	s, _, m := frameTestSessionWithController(t, []string{"/bin/cat"}, true)
	ctx := context.Background()
	setRuntimeReady(t, m, s)
	threadID := "thread-1"
	s.mu.Lock()
	s.record.NativeSessionID = &threadID
	s.mu.Unlock()
	ptr := componentResource(s.id, Runtime{}).Metadata()
	if _, err := safe.StateUpdateWithConflicts(ctx, s.deps.state, ptr, func(value *typed.Resource[Runtime, componentExtension[Runtime]]) error {
		value.TypedSpec().Resuming = true
		return nil
	}, state.WithUpdateOwner(ControllerName)); err != nil {
		t.Fatal(err)
	}
	adapter := codexadapter.New(s)
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "finished-turn"}), nil); err != nil {
		t.Fatal(err)
	}
	if err := s.Emit(ctx, observation(&eventpb.TurnCompleted{TurnId: "finished-turn", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}), nil); err != nil {
		t.Fatal(err)
	}
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "active-successor"}), nil); err != nil {
		t.Fatal(err)
	}
	before := s.journal.LastCursor()
	frame := []byte(`{"method":"turn/completed","params":{"threadId":"thread-1","turn":{"id":"finished-turn","status":"completed"}}}`)
	if err := s.receive(ctx, frame, adapter); err != nil {
		t.Fatal(err)
	}
	entries, err := s.journal.Since(ctx, before, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Event.GetNative() == nil || entries[0].Event.GetNative().GetLine() != string(frame) {
		t.Fatalf("stale native frame entries = %v, want its raw Native frame only", entries)
	}
	s.mu.Lock()
	activeTurn := s.activeTurn
	s.mu.Unlock()
	if activeTurn != "active-successor" || s.journal.RecoveryState().ActiveTurnID != "active-successor" {
		t.Fatalf("stale completion cleared successor: session=%q journal=%+v", activeTurn, s.journal.RecoveryState())
	}
}

func TestStaleTurnCompletionsHaveNoSessionSideEffects(t *testing.T) {
	s, _, _ := frameTestSession(t, []string{"/bin/cat"})
	ctx := context.Background()
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "active-successor"}), nil); err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	s.adapter = frameTestAdapter{}
	s.interrupts["finished-turn"] = "interrupt-command"
	s.nativeStop = stopQuiesce
	s.mu.Unlock()
	before := s.journal.LastCursor()
	staleInterrupt := &eventpb.TurnCompleted{TurnId: "finished-turn", Status: eventpb.TurnStatus_TURN_STATUS_INTERRUPTED}
	if err := s.Emit(ctx, observation(staleInterrupt), nil); err != nil {
		t.Fatal(err)
	}
	staleLoss := &eventpb.TurnCompleted{TurnId: "unknown-turn", Status: eventpb.TurnStatus_TURN_STATUS_PROCESS_LOST}
	if err := s.Emit(ctx, observation(staleLoss), nil); err != nil {
		t.Fatal(err)
	}
	if s.journal.LastCursor() != before {
		t.Fatalf("stale completions advanced cursor from %d to %d", before, s.journal.LastCursor())
	}
	s.mu.Lock()
	activeTurn := s.activeTurn
	interrupt := s.interrupts["finished-turn"]
	reconciliations := len(s.reconciliations)
	s.mu.Unlock()
	if activeTurn != "active-successor" || interrupt != "interrupt-command" || reconciliations != 0 {
		t.Fatalf("stale completion side effects: active=%q interrupt=%q reconciliations=%d", activeTurn, interrupt, reconciliations)
	}
	if staleInterrupt.GetInterruptedByCommandId() != "" || staleInterrupt.GetEndedByTeardown() || staleLoss.GetInterruptedByCommandId() != "" || staleLoss.GetEndedByTeardown() {
		t.Fatalf("stale completion messages were mutated: interrupted=%v lost=%v", staleInterrupt, staleLoss)
	}
	entries, err := s.journal.Since(ctx, before, 10)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.Event.GetConversationReconciled() != nil || entry.Event.GetTurnCompleted() != nil {
			t.Fatalf("stale completion created a derived event: %v", entry)
		}
	}
}

func TestConcurrentCompletionAndSuccessorStartPreserveSuccessor(t *testing.T) {
	s, _, _ := frameTestSession(t, []string{"/bin/cat"})
	ctx := context.Background()
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "current"}), nil); err != nil {
		t.Fatal(err)
	}
	start := make(chan struct{})
	errs := make(chan error, 2)
	var workers sync.WaitGroup
	workers.Add(2)
	go func() {
		defer workers.Done()
		<-start
		errs <- s.Emit(ctx, observation(&eventpb.TurnCompleted{TurnId: "current", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}), nil)
	}()
	go func() {
		defer workers.Done()
		<-start
		errs <- s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "successor"}), nil)
	}()
	close(start)
	workers.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	s.mu.Lock()
	activeTurn := s.activeTurn
	s.mu.Unlock()
	if activeTurn != "successor" || s.journal.RecoveryState().ActiveTurnID != "successor" {
		t.Fatalf("concurrent transition lost successor: session=%q journal=%+v", activeTurn, s.journal.RecoveryState())
	}
}

func TestTurnStartedReceiveBatchDoesNotDeadlockCompletion(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	j, err := journal.Open(ctx, path, "source")
	if err != nil {
		t.Fatal(err)
	}
	deadlocked := false
	defer func() {
		if !deadlocked {
			if err := j.Close(); err != nil {
				t.Error(err)
			}
		}
	}()
	s := newSession(sessionDependencies{
		ctx: ctx, publish: func(*Session, bool) {}, changed: func() {}, fail: func(err error) { t.Error(err) },
	}, "session", &retained.Record{}, j)
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "current"}), nil); err != nil {
		t.Fatal(err)
	}
	inAdapter, resumeAdapter := make(chan struct{}), make(chan struct{})
	adapter := frameTestAdapter{onFrame: func(batchCtx context.Context, _ json.RawMessage, sequence uint64) error {
		close(inAdapter)
		<-resumeAdapter
		return s.Emit(batchCtx, observation(&eventpb.TurnStarted{TurnId: "successor"}), []uint64{sequence})
	}}
	frame := []byte(`{"type":"turn/started"}`)
	receiveDone := make(chan error, 1)
	go func() {
		receiveDone <- s.connection.receiveBatch(ctx, [][]byte{frame}, func(batchCtx context.Context, line []byte) error {
			return s.receive(batchCtx, line, adapter)
		})
	}()
	select {
	case <-inAdapter: // receiveBatch owns the journal lock after appending the raw Native frame.
	case <-time.After(2 * time.Second):
		deadlocked = true
		t.Fatal("native receive did not reach adapter barrier")
	}
	completionDone := make(chan error, 1)
	go func() {
		completionDone <- s.completeTurn(ctx, &eventpb.TurnCompleted{TurnId: "current", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}, nil)
	}()
	lockDeadline := time.NewTimer(2 * time.Second)
	defer lockDeadline.Stop()
	for {
		if s.writeMu.TryLock() {
			s.writeMu.Unlock()
		} else {
			break // completeTurn owns writeMu and is blocked on the journal batch.
		}
		select {
		case <-lockDeadline.C:
			deadlocked = true
			t.Fatal("completion did not reach the write gate")
		default:
			time.Sleep(time.Millisecond)
		}
	}
	close(resumeAdapter)
	select {
	case err := <-receiveDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		deadlocked = true
		t.Fatal("native receive batch deadlocked against completion")
	}
	select {
	case err := <-completionDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		deadlocked = true
		t.Fatal("turn completion deadlocked against native receive batch")
	}
	if got := j.RecoveryState().ActiveTurnID; got != "successor" {
		t.Fatalf("active turn after concurrent transitions = %q, want successor", got)
	}
	entries, err := j.Since(ctx, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	var nativeCursor uint64
	var successor *eventlogpb.EventEntry
	for _, entry := range entries {
		if entry.Event.GetNative() != nil && entry.Event.GetNative().GetLine() == string(frame) {
			nativeCursor = entry.Cursor
		}
		if entry.Event.GetTurnStarted() != nil && entry.Event.GetTurnStarted().GetTurnId() == "successor" {
			successor = entry
		}
	}
	if nativeCursor == 0 || successor == nil || len(successor.Event.GetSourceSequences()) != 1 || successor.Event.GetSourceSequences()[0] != nativeCursor {
		t.Fatalf("raw Native/source attribution lost: native_cursor=%d successor=%v", nativeCursor, successor)
	}
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
		s.connection.readers.Wait()
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
	s.connection.readers.Add(1)
	go s.connection.readStdout(p, func(ctx context.Context, line []byte) error { return s.receive(ctx, line, adapter) })
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
	s.connection.readers.Add(1)
	go s.connection.readStdout(p, func(ctx context.Context, line []byte) error { return s.receive(ctx, line, adapter) })
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
	s.connection.readers.Add(1)
	go s.connection.readStdout(p, func(ctx context.Context, line []byte) error { return s.receive(ctx, line, adapter) })
	s.connection.readers.Wait()
	entries, err := s.journal.Since(context.Background(), 0, 128)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 || entries[0].GetEvent().GetNative().Line != `{"final":true}` || entries[1].GetEvent().GetHarnessExited() == nil {
		t.Fatalf("final native frame was lost or changed: %v", entries)
	}
}

func TestHarnessExitPublishesTurnRecoveryBeforeTerminalEvent(t *testing.T) {
	ctx := context.Background()
	s, p, _ := frameTestSession(t, []string{"/bin/cat"})
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "lost-turn"}), nil); err != nil {
		t.Fatal(err)
	}
	if err := s.Emit(ctx, observation(&eventpb.ItemStarted{ItemId: "unfinished-tool", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "Bash"}), nil); err != nil {
		t.Fatal(err)
	}
	adapter := frameTestAdapter{onFrame: func(context.Context, json.RawMessage, uint64) error { return nil }}
	s.connection.startStdout(p, func(ctx context.Context, line []byte) error { return s.receive(ctx, line, adapter) })
	if err := p.Stop(ctx); err != nil {
		t.Fatal(err)
	}
	s.connection.join()
	entries, err := s.journal.Since(ctx, 0, 128)
	if err != nil {
		t.Fatal(err)
	}
	completed, reconciled, exited := false, false, false
	for _, entry := range entries {
		event := entry.GetEvent()
		if turn := event.GetTurnCompleted(); turn != nil && turn.TurnId == "lost-turn" {
			completed = turn.Status == eventpb.TurnStatus_TURN_STATUS_PROCESS_LOST
		}
		if report := event.GetConversationReconciled(); report != nil && report.TurnId == "lost-turn" {
			reconciled = len(report.Items) == 1 && report.Items[0].ItemId == "unfinished-tool" && report.Items[0].Disposition == eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN
		}
		if event.GetHarnessExited() != nil {
			exited = true
			if !completed || !reconciled {
				t.Fatalf("terminal harness exit preceded turn recovery: completed=%t reconciled=%t", completed, reconciled)
			}
		}
	}
	if !exited {
		t.Fatal("missing terminal harness exit")
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
	s.connection.readers.Add(1)
	go s.connection.readStdout(p, func(ctx context.Context, line []byte) error { return s.receive(ctx, line, adapter) })
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

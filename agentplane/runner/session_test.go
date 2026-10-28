package runner

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/state"

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

func (a *pendingSubmitAdapter) Submit(context.Context, string, string) error {
	a.submits++
	return &harness.NativeResponsePendingError{Cause: context.DeadlineExceeded}
}

func (a frameTestAdapter) OnFrame(ctx context.Context, raw json.RawMessage, sequence uint64) error {
	return a.onFrame(ctx, raw, sequence)
}

func frameTestSession(t *testing.T, command []string) (*Session, *supervision.Process) {
	t.Helper()
	r, err := New(Config{StateDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	record, err := NewSessionRecord(&runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: t.TempDir(), Model: "test"}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.store.Write("session", record); err != nil {
		t.Fatal(err)
	}
	dir, err := r.store.Directory("session")
	if err != nil {
		t.Fatal(err)
	}
	journal, err := OpenJournal(context.Background(), filepath.Join(dir, "journal.sqlite"), record.EventSourceID)
	if err != nil {
		t.Fatal(err)
	}
	s := newSession(r, "session", &record, journal)
	p, err := r.supervisor.NewProcess(supervision.ProcessConfig{Command: command, Dir: record.CWD, Env: map[string]string{}})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	s.process = p
	t.Cleanup(func() {
		if err := p.Stop(context.Background(), time.Second); err != nil {
			t.Error(err)
		}
		r.cancel()
		<-r.lifecycle.done
		r.lifecycle.workers.Wait()
		s.work.Wait()
		s.readers.Wait()
		if err := journal.Close(); err != nil {
			t.Error(err)
		}
		if err := r.owner.Close(); err != nil {
			t.Error(err)
		}
	})
	return s, p
}

func TestNativeReplyWaitsForTranslationCommit(t *testing.T) {
	s, p := frameTestSession(t, []string{"/bin/sh", "-c", `while IFS= read -r line; do printf '%s\n' "$line"; done`})
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
	s, _ := frameTestSession(t, []string{"/bin/cat"})
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
	// This primitive test drives dispatch directly, so it has no Session input
	// for the controller to reconcile. Seed only its readiness observation.
	if err := s.runner.lifecycle.state.Create(ctx,
		lifecycleResourceFor(s.id, sessionRuntimeSpec{Phase: executionReady}),
		state.WithCreateOwner(sessionControllerName)); err != nil {
		t.Fatal(err)
	}
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
	s, _ := frameTestSession(t, []string{"/bin/cat"})
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
	s, p := frameTestSession(t, []string{"/bin/sh", "-c", script})
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
	s, p := frameTestSession(t, []string{"/bin/sh", "-c", `printf '{"final":true}\r'`})
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
	r := newLifecycleRunner(t, Config{StateDir: t.TempDir()})
	record, err := NewSessionRecord(&runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: t.TempDir(), Model: "test-model"}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	s := newSession(r, "session-1", &record, journal)
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
	s, p := frameTestSession(t, []string{"/bin/sh", "-c", `while IFS= read -r line; do printf '%s\n' "$line"; done`})
	adapter := &rpcReconciliationAdapter{session: s}
	s.mu.Lock()
	s.adapter = adapter
	s.mu.Unlock()
	registerTestSession(t, s)
	setRuntimeReady(t, s)
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

func TestObservedItemKeepsOwningTurnWhenItCompletesLater(t *testing.T) {
	journal := openTestJournal(t)
	ctx := context.Background()
	for _, event := range []*eventpb.Event{
		observation(&eventpb.TurnStarted{TurnId: "owner"}),
		observation(&eventpb.ItemStarted{ItemId: "tool", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "commandExecution"}),
		observation(&eventpb.TurnCompleted{TurnId: "owner", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}),
		observation(&eventpb.TurnStarted{TurnId: "later"}),
		observation(&eventpb.ItemCompleted{ItemId: "tool", Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: "done", Succeeded: true}}}),
		observation(&eventpb.TurnCompleted{TurnId: "later", Status: eventpb.TurnStatus_TURN_STATUS_INTERRUPTED}),
	} {
		if _, err := journal.Append(ctx, event, AppendOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	items, err := journal.ObservedItems(ctx, "later")
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items["tool"].TurnID != "owner" || !items["tool"].Completed || items["tool"].Output != "done" {
		t.Fatalf("lost item owner or later completion: %+v", items)
	}
}

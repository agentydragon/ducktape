package runner

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"

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

func frameTestSession(t *testing.T, command []string) (*Session, *Process) {
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
	p, err := NewProcess(ProcessConfig{Command: command, Dir: record.CWD, Env: map[string]string{}, StateOwnerFD: r.owner.File()})
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
	s.ready = true
	s.adapter = adapter
	s.mu.Unlock()
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
	release := filepath.Join(t.TempDir(), "release")
	script := `while IFS= read -r line; do while [ ! -e "$1" ]; do sleep 0.01; done; printf '%s\n' "$line"; done`
	s, p := frameTestSession(t, []string{"/bin/sh", "-c", script, "--", release})
	received := make(chan struct{})
	adapter := frameTestAdapter{onFrame: func(ctx context.Context, raw json.RawMessage, sequence uint64) error {
		if err := s.Emit(ctx, observation(&eventpb.HarnessStderr{Text: string(raw)}), []uint64{sequence}); err != nil {
			return err
		}
		close(received)
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
	if err := s.journal.WaitBeyond(waitCtx, 0); err != nil {
		t.Fatal(err)
	}
	cancel()
	if err := <-done; !errors.Is(err, harness.ErrNativeResponsePending) {
		t.Fatalf("cancelled native wait error = %v", err)
	}
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	select {
	case <-received:
	case <-waitCtx.Done():
		t.Fatal("late native response was not delivered to the adapter")
	}
	entries, err := s.journal.Since(context.Background(), 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 3 || entries[2].GetEvent().GetHarnessStderr() == nil {
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

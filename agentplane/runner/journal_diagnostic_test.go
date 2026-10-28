package runner

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

func TestJournalDiagnosticIsReadOnlyRedactedAndBounded(t *testing.T) {
	ctx := context.Background()
	stateDir := t.TempDir()
	sessionID := "session-1"
	store, err := NewSessionStore(filepath.Join(stateDir, "sessions"))
	if err != nil {
		t.Fatal(err)
	}
	nativeSessionID := "native-session-17"
	record := SessionRecord{
		Harness: runnerpb.Harness_HARNESS_CODEX, CWD: "/state/work", Instructions: "PRIVATE_INSTRUCTIONS",
		EventSourceID: "event-source-1", NativeSessionID: &nativeSessionID,
	}
	if err := store.Write(sessionID, record); err != nil {
		t.Fatal(err)
	}
	journalPath := filepath.Join(stateDir, "sessions", sessionID, "journal.sqlite")
	journal, err := OpenJournal(ctx, journalPath, record.EventSourceID)
	if err != nil {
		t.Fatal(err)
	}
	defer journal.Close()
	var journalMode string
	if err := journal.conn.QueryRowContext(ctx, "PRAGMA journal_mode=WAL").Scan(&journalMode); err != nil {
		t.Fatal(err)
	}
	if journalMode != "wal" {
		t.Fatalf("journal mode = %q, want WAL", journalMode)
	}
	if _, err := journal.conn.ExecContext(ctx, "PRAGMA wal_autocheckpoint=0"); err != nil {
		t.Fatal(err)
	}
	commandID := "00000000-0000-4000-8000-000000000001"
	command := &commandpb.Command{
		CommandId: commandID,
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "PRIVATE_PROMPT"}},
	}
	if _, _, err := journal.Admit(ctx, command, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := journal.DispatchPlanned(ctx, commandID); err != nil {
		t.Fatal(err)
	}
	if _, err := journal.Append(ctx, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{
		TurnId: "turn-1",
	}}}, AppendOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := journal.Append(ctx, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{
		TurnId: "turn-1", Status: eventpb.TurnStatus_TURN_STATUS_FAILED, Error: "PRIVATE_ERROR_TEXT",
	}}}, AppendOptions{}); err != nil {
		t.Fatal(err)
	}
	const noisyRows = 300
	if err := journal.WithBatch(ctx, func(batchCtx context.Context) error {
		for i := 0; i < noisyRows; i++ {
			if _, err := journal.Append(batchCtx, &eventpb.Event{
				Observation: &eventpb.Event_Native{Native: &eventpb.Native{
					Direction: eventpb.Direction_DIRECTION_TO_HARNESS,
					Line:      `{"method":"PRIVATE_METHOD","params":{"text":"PRIVATE_NATIVE_PAYLOAD"}}`,
				}},
			}, AppendOptions{}); err != nil {
				return err
			}
		}
		if _, err := journal.Append(batchCtx, &eventpb.Event{
			Observation: &eventpb.Event_Native{Native: &eventpb.Native{
				Direction: eventpb.Direction_DIRECTION_FROM_HARNESS,
				Line:      `{"method":"thread/start","id":42,"error":{"code":-32000,"message":"PRIVATE_NATIVE_ERROR"}}`,
			}},
		}, AppendOptions{}); err != nil {
			return err
		}
		if _, err := journal.Append(batchCtx, &eventpb.Event{
			Observation: &eventpb.Event_CommandFailed{CommandFailed: &eventpb.CommandFailed{
				CommandId: commandID, Reason: "PRIVATE_TERMINAL_REASON",
			}},
		}, AppendOptions{TerminalCommandIDs: []string{commandID}}); err != nil {
			return err
		}
		_, err := journal.Append(batchCtx, &eventpb.Event{
			Observation: &eventpb.Event_Native{Native: &eventpb.Native{
				Direction: eventpb.Direction_DIRECTION_FROM_HARNESS,
				Line:      strings.Repeat("x", journalSnapshotPayloadLimit),
			}},
		}, AppendOptions{})
		return err
	}); err != nil {
		t.Fatal(err)
	}
	walPath := journalPath + "-wal"
	walBefore, err := os.Stat(walPath)
	if err != nil {
		t.Fatal(err)
	}
	if walBefore.Size() == 0 {
		t.Fatal("test journal has no uncheckpointed WAL data")
	}
	before, err := os.Stat(journalPath)
	if err != nil {
		t.Fatal(err)
	}

	var snapshot journalDiagnostic
	if err := captureJournalDiagnostic(ctx, stateDir, sessionID, journalSnapshotEntryLimit, []string{commandID}, &snapshot); err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	output := string(encoded)
	for _, private := range []string{
		"PRIVATE_PROMPT", "PRIVATE_METHOD", "PRIVATE_NATIVE_PAYLOAD", "PRIVATE_NATIVE_ERROR", "PRIVATE_TERMINAL_REASON",
		"PRIVATE_ERROR_TEXT", "PRIVATE_INSTRUCTIONS",
	} {
		if strings.Contains(output, private) {
			t.Fatalf("journal diagnostic leaked private content marker %q", private)
		}
	}
	if !strings.Contains(output, nativeSessionID) || !strings.Contains(output, `"native_method":"thread/start"`) ||
		!strings.Contains(output, `"native_request_id":"42"`) || !strings.Contains(output, `"native_error_kind":"json_rpc_error"`) ||
		!strings.Contains(output, `"native_error_code":"-32000"`) {
		t.Fatal("journal diagnostic missed bounded metadata evidence")
	}
	if len(snapshot.Entries) != journalSnapshotEntryLimit || snapshot.Entries[253].Kind != "native_error" ||
		snapshot.Entries[253].NativeErrorCode != "-32000" || snapshot.Entries[253].NativeFrameBytes == 0 {
		t.Fatalf("native frame metadata = %+v", snapshot.Entries)
	}
	if snapshot.EarliestIndexedFailure == nil || snapshot.EarliestIndexedFailure.Kind != "turn_completed" ||
		snapshot.EarliestIndexedFailure.Cursor >= snapshot.Entries[0].Cursor {
		t.Fatalf("earliest indexed failure was not retained separately from the recent tail: %+v", snapshot.EarliestIndexedFailure)
	}
	if !snapshot.EntriesTruncated || len(snapshot.Entries) != journalSnapshotEntryLimit ||
		!snapshot.Entries[len(snapshot.Entries)-1].PayloadOmitted ||
		snapshot.Entries[len(snapshot.Entries)-1].PayloadBytes <= journalSnapshotPayloadLimit || len(output) > 100_000 {
		t.Fatalf("oversized journal payload was not omitted: entry=%+v output_bytes=%d", snapshot.Entries[len(snapshot.Entries)-1], len(output))
	}
	if len(snapshot.NamedCommands) != 1 {
		t.Fatalf("named command diagnostics = %+v", snapshot.NamedCommands)
	}
	named := snapshot.NamedCommands[0]
	if named.CommandID != commandID || named.Status != "admitted" || named.Operation != "submit_input" ||
		named.AdmittedCursor >= snapshot.Entries[0].Cursor || !named.DispatchPlanned || named.TerminalCursor == nil ||
		named.TerminalKind != "command_failed" {
		t.Fatalf("named command lifecycle was not retained independently of the tail: %+v", named)
	}
	if snapshot.Checkpoint == nil || snapshot.Checkpoint.ThroughCursor < snapshot.Entries[len(snapshot.Entries)-1].Cursor {
		t.Fatalf("checkpoint missed WAL-backed entries: %+v", snapshot.Checkpoint)
	}
	after, err := os.Stat(journalPath)
	if err != nil {
		t.Fatal(err)
	}
	if !before.ModTime().Equal(after.ModTime()) {
		t.Fatalf("read-only snapshot changed journal mtime: before=%v after=%v", before.ModTime(), after.ModTime())
	}
	walAfter, err := os.Stat(walPath)
	if err != nil {
		t.Fatal(err)
	}
	if before.Size() != after.Size() || walBefore.Size() != walAfter.Size() || !walBefore.ModTime().Equal(walAfter.ModTime()) {
		t.Fatalf("read-only snapshot changed SQLite files: db=%d/%d wal=%d/%d", before.Size(), after.Size(), walBefore.Size(), walAfter.Size())
	}
	if _, err := journal.Append(ctx, &eventpb.Event{Observation: &eventpb.Event_Native{Native: &eventpb.Native{
		Direction: eventpb.Direction_DIRECTION_TO_HARNESS, Line: `{"method":"thread/after_snapshot"}`,
	}}}, AppendOptions{}); err != nil {
		t.Fatalf("journal writer could not append after diagnostic reader: %v", err)
	}
}

func TestJournalDiagnosticFailureIsStageCodedAndDoesNotCreateJournal(t *testing.T) {
	stateDir := t.TempDir()
	store, err := NewSessionStore(filepath.Join(stateDir, "sessions"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Write("session-1", SessionRecord{Harness: runnerpb.Harness_HARNESS_CODEX, EventSourceID: "source-1"}); err != nil {
		t.Fatal(err)
	}
	var snapshot journalDiagnostic
	err = captureJournalDiagnostic(context.Background(), stateDir, "session-1", 10, nil, &snapshot)
	if err == nil || err.Error() != "journal_connection_failed" {
		t.Fatalf("capture error = %v, want stage-coded journal_connection_failed", err)
	}
	if _, err := os.Stat(filepath.Join(stateDir, "sessions", "session-1", "journal.sqlite")); !os.IsNotExist(err) {
		t.Fatalf("read-only diagnostic created journal file: stat error = %v", err)
	}
}

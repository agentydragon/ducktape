package runner

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

var legacyJournalSchema = []string{
	`CREATE TABLE IF NOT EXISTS event_entry (
		cursor INTEGER NOT NULL PRIMARY KEY,
		payload BLOB NOT NULL
	)`,
	`CREATE TABLE IF NOT EXISTS command (
		command_id VARCHAR NOT NULL PRIMARY KEY,
		payload BLOB NOT NULL,
		admitted_cursor INTEGER NOT NULL REFERENCES event_entry(cursor),
		dispatch_planned BOOLEAN NOT NULL,
		native_correlation JSON NOT NULL,
		terminal_cursor INTEGER REFERENCES event_entry(cursor)
	)`,
	`CREATE INDEX IF NOT EXISTS pending_command_admission ON command (terminal_cursor, admitted_cursor)`,
	`CREATE TABLE IF NOT EXISTS turn_boundary (
		turn_id VARCHAR NOT NULL PRIMARY KEY,
		start_cursor INTEGER NOT NULL,
		end_cursor INTEGER,
		status INTEGER
	)`,
	`CREATE INDEX IF NOT EXISTS ix_turn_boundary_start_cursor ON turn_boundary (start_cursor)`,
	`CREATE TABLE IF NOT EXISTS checkpoint (
		id INTEGER NOT NULL PRIMARY KEY,
		source_id VARCHAR NOT NULL,
		through_cursor INTEGER NOT NULL,
		harness_running BOOLEAN NOT NULL,
		active_turn_id VARCHAR NOT NULL
	)`,
	`CREATE TABLE IF NOT EXISTS seal (
		id INTEGER NOT NULL PRIMARY KEY,
		cursor INTEGER NOT NULL,
		incarnation_uid VARCHAR NOT NULL
	)`,
	`CREATE TABLE IF NOT EXISTS debug_checkpoint (
		name VARCHAR NOT NULL,
		command_id VARCHAR NOT NULL,
		PRIMARY KEY (name, command_id)
	)`,
}

func TestOpenJournalReopensPythonSchemaAndKeepsStoredBytes(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	for _, statement := range legacyJournalSchema {
		if _, err := db.Exec(statement); err != nil {
			t.Fatalf("create legacy schema: %v", err)
		}
	}
	legacyEntry := &eventlogpb.EventEntry{
		Cursor: 1,
		Origin: &eventlogpb.EventOrigin{SourceId: "stable-source", Sequence: 1},
		Event:  &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{Resumed: true}}},
	}
	legacyBytes, err := proto.Marshal(legacyEntry)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("INSERT INTO event_entry (cursor, payload) VALUES (1, ?)", legacyBytes); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("INSERT INTO checkpoint VALUES (1, 'stable-source', 1, 1, '')"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	journal, err := OpenJournal(ctx, path, "stable-source")
	if err != nil {
		t.Fatal(err)
	}
	defer journal.Close()
	if journal.LastCursor() != 1 || !journal.RecoveryState().HarnessRunning {
		t.Fatalf("recovered state = %+v", journal.RecoveryState())
	}
	entries, err := journal.Since(ctx, 0, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || !proto.Equal(entries[0], legacyEntry) {
		t.Fatalf("reopened legacy entries = %v", entries)
	}
	var retained []byte
	if err := journal.conn.QueryRowContext(ctx, "SELECT payload FROM event_entry WHERE cursor = 1").Scan(&retained); err != nil {
		t.Fatal(err)
	}
	if string(retained) != string(legacyBytes) {
		t.Fatal("reopening rewrote the retained EventEntry blob")
	}
	var version int64
	if err := journal.conn.QueryRowContext(ctx, "SELECT MAX(version_id) FROM "+journalMigrationTableName).Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 1 {
		t.Fatalf("Goose migration version = %d, want 1", version)
	}
}

func TestAdmissionDeduplicatesFullCommandAndRejectsConflicts(t *testing.T) {
	journal := openTestJournal(t)
	ctx := context.Background()
	command := &commandpb.Command{
		CommandId: "input-1",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}},
	}
	entry, admitted, err := journal.Admit(ctx, command, "")
	if err != nil || !admitted || entry.GetCursor() != 1 {
		t.Fatalf("first admission = (%v, %t, %v)", entry, admitted, err)
	}
	duplicate, admitted, err := journal.Admit(ctx, proto.Clone(command).(*commandpb.Command), "")
	if err != nil || admitted || duplicate == nil || !proto.Equal(duplicate, entry) || journal.LastCursor() != 1 {
		t.Fatalf("duplicate admission = (%v, %t, %v), first=%v cursor=%d", duplicate, admitted, err, entry, journal.LastCursor())
	}
	conflict := proto.Clone(command).(*commandpb.Command)
	conflict.GetSubmitInput().Text = "different"
	if _, _, err := journal.Admit(ctx, conflict, ""); !errors.Is(err, ErrCommandConflict) {
		t.Fatalf("conflicting admission error = %v", err)
	}
	if journal.LastCursor() != 1 {
		t.Fatalf("conflicting admission advanced cursor to %d", journal.LastCursor())
	}
}

func TestTurnTargetedAdmissionRejectsStaleInterrupt(t *testing.T) {
	journal := openTestJournal(t)
	ctx := context.Background()
	if _, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "old-turn"}},
	}, AppendOptions{}); err != nil {
		t.Fatal(err)
	}
	for _, event := range []*eventpb.Event{
		{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: "old-turn", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}}},
		{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "next-turn"}}},
	} {
		if _, err := journal.Append(ctx, event, AppendOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	interrupt := &commandpb.Command{
		CommandId: "interrupt-old",
		Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "old-turn"}},
	}
	if _, _, err := journal.Admit(ctx, interrupt, "old-turn"); !errors.Is(err, ErrActiveTurnChanged) {
		t.Fatalf("stale interrupt error = %v", err)
	}
	entries, err := journal.Since(ctx, 0, 8)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.GetEvent().GetCommandAdmitted() != nil {
			t.Fatal("stale interrupt was admitted")
		}
	}
	if journal.LastCursor() != 3 {
		t.Fatalf("stale interrupt advanced cursor to %d", journal.LastCursor())
	}
}

func TestDuplicateInterruptReturnsOriginalAdmissionAfterTurnEndsAndSeal(t *testing.T) {
	ctx := context.Background()
	journal := openTestJournal(t)
	if _, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "turn-1"}},
	}, AppendOptions{}); err != nil {
		t.Fatal(err)
	}
	command := &commandpb.Command{
		CommandId: "interrupt-1",
		Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "turn-1"}},
	}
	original, admitted, err := journal.Admit(ctx, command, "turn-1")
	if err != nil || !admitted {
		t.Fatalf("initial interrupt admission = (%v, %t, %v)", original, admitted, err)
	}
	if _, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{
			TurnId: "turn-1", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED,
		}},
	}, AppendOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := journal.Seal(ctx, "incarnation"); err != nil {
		t.Fatal(err)
	}

	retried, admitted, err := journal.Admit(ctx, proto.Clone(command).(*commandpb.Command), "turn-1")
	if err != nil || admitted || retried == nil || !proto.Equal(retried, original) {
		t.Fatalf("duplicate interrupt admission = (%v, %t, %v), original %v", retried, admitted, err, original)
	}
	conflicting := proto.Clone(command).(*commandpb.Command)
	conflicting.GetInterruptTurn().TurnId = "another-turn"
	if _, _, err := journal.Admit(ctx, conflicting, "another-turn"); !errors.Is(err, ErrCommandConflict) {
		t.Fatalf("conflicting stale interrupt error = %v", err)
	}
	fresh := proto.Clone(command).(*commandpb.Command)
	fresh.CommandId = "interrupt-2"
	if _, _, err := journal.Admit(ctx, fresh, "turn-1"); !errors.Is(err, ErrJournalSealed) {
		t.Fatalf("fresh interrupt after seal error = %v", err)
	}
}

func TestOneEventCoalescesCommandReceiptsAtomically(t *testing.T) {
	journal := openTestJournal(t)
	ctx := context.Background()
	for _, id := range []string{"input-a", "input-b"} {
		command := &commandpb.Command{
			CommandId: id,
			Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: id}},
		}
		if _, _, err := journal.Admit(ctx, command, ""); err != nil {
			t.Fatal(err)
		}
	}
	event := &eventpb.Event{
		Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
			OriginCommandIds: []string{"input-a", "input-b"}, Text: "combined",
		}},
	}
	receipt, err := journal.Append(ctx, event, AppendOptions{
		TerminalCommandIDs: []string{"input-a", "input-b"},
		ReceiptMetadata:    CommandReceiptMetadata{HarnessMessageID: "native-7", TurnID: "turn-1"},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"input-a", "input-b"} {
		command, err := journal.GetCommand(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		if command == nil || command.TerminalCursor == nil || *command.TerminalCursor != receipt.GetCursor() || command.ReceiptMetadata.HarnessMessageID != "native-7" || command.ReceiptMetadata.TurnID != "turn-1" {
			t.Fatalf("receipt for %s = %+v", id, command)
		}
	}
	before := journal.LastCursor()
	_, err = journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{Text: "orphan"}},
	}, AppendOptions{TerminalCommandIDs: []string{"input-a", "missing"}})
	if !errors.Is(err, ErrUnknownCommand) {
		t.Fatalf("unknown receipt error = %v", err)
	}
	if journal.LastCursor() != before {
		t.Fatalf("rejected receipt append published cursor %d; want %d", journal.LastCursor(), before)
	}
}

func TestNativeRequestCorrelationSurvivesJournalReopen(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	command := &commandpb.Command{
		CommandId: "input-1",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}},
	}
	journal, err := OpenJournal(ctx, path, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := journal.Admit(ctx, command, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := journal.DispatchPlanned(ctx, command.CommandId); err != nil {
		t.Fatal(err)
	}
	if err := journal.RecordNativeCorrelation(ctx, command.CommandId, harness.NativeCorrelation{
		Adapter: "codex", SchemaVersion: 1,
		Payload: json.RawMessage(`{"turn_start":{"request_id":"agentplane-test-request"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := OpenJournal(ctx, path, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	pending, err := reopened.PendingCommandInfo(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 1 || pending[0].CommandID != command.CommandId || pending[0].SubmitText != "hello" || !pending[0].DispatchPlanned || pending[0].NativeCorrelation == nil || pending[0].NativeCorrelation.Adapter != "codex" || string(pending[0].NativeCorrelation.Payload) != `{"turn_start":{"request_id":"agentplane-test-request"}}` {
		t.Fatalf("recovered native request correlation = %+v", pending)
	}
}

func TestJournalMigratesLegacyNativeCorrelationMap(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	command := &commandpb.Command{
		CommandId: "input-legacy",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}},
	}
	journal, err := OpenJournal(ctx, path, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := journal.Admit(ctx, command, ""); err != nil {
		t.Fatal(err)
	}
	legacy := `{"codex.turn_start.request_id":"legacy-request","harness_message_id":"native-legacy","turn_id":"turn-legacy"}`
	if _, err := journal.conn.ExecContext(ctx, "UPDATE command SET native_correlation = ? WHERE command_id = ?", legacy, command.CommandId); err != nil {
		t.Fatal(err)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("DROP TABLE " + journalMigrationTableName); err != nil {
		_ = db.Close()
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := OpenJournal(ctx, path, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	record, err := reopened.GetCommand(ctx, command.CommandId)
	if err != nil {
		t.Fatal(err)
	}
	var oldFields map[string]string
	if record == nil || json.Unmarshal(record.LegacyNativeCorrelation, &oldFields) != nil || oldFields["codex.turn_start.request_id"] != "legacy-request" || record.ReceiptMetadata.HarnessMessageID != "native-legacy" || record.ReceiptMetadata.TurnID != "turn-legacy" {
		t.Fatalf("migrated legacy command data = %+v, old fields = %+v", record, oldFields)
	}
	var stored string
	if err := reopened.conn.QueryRowContext(ctx, "SELECT native_correlation FROM command WHERE command_id = ?", command.CommandId).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	var versioned commandJournalData
	if err := json.Unmarshal([]byte(stored), &versioned); err != nil || versioned.Version != commandJournalDataVersion {
		t.Fatalf("stored command data = %s, decode error = %v", stored, err)
	}
}

func TestJournalBatchPublishesAtCommitBoundary(t *testing.T) {
	journal := openTestJournal(t)
	ctx := context.Background()
	err := journal.WithBatch(ctx, func(batchCtx context.Context) error {
		first, err := journal.Append(batchCtx, &eventpb.Event{
			Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "batch-turn"}},
		}, AppendOptions{})
		if err != nil {
			return err
		}
		if first.GetCursor() != 1 || journal.LastCursor() != 0 {
			return errors.New("batch exposed an uncommitted cursor")
		}
		if err := journal.CommitBatch(batchCtx); err != nil {
			return err
		}
		if journal.LastCursor() != 1 || journal.RecoveryState().ActiveTurnID != "batch-turn" {
			return errors.New("batch commit did not publish its checkpoint")
		}
		_, err = journal.Append(batchCtx, &eventpb.Event{
			Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{
				TurnId: "batch-turn", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED,
			}},
		}, AppendOptions{})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if journal.LastCursor() != 2 || journal.RecoveryState().ActiveTurnID != "" {
		t.Fatalf("batch final state = %+v", journal.RecoveryState())
	}
}

func TestFailedJournalWriteDoesNotPublishCursor(t *testing.T) {
	journal := openTestJournal(t)
	ctx := context.Background()
	if _, err := journal.conn.ExecContext(ctx, `CREATE TRIGGER reject_event BEFORE INSERT ON event_entry BEGIN SELECT RAISE(ABORT, 'blocked write'); END`); err != nil {
		t.Fatal(err)
	}
	_, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_HarnessLost{HarnessLost: &eventpb.HarnessLost{}},
	}, AppendOptions{})
	if err == nil {
		t.Fatal("expected injected journal write failure")
	}
	if journal.LastCursor() != 0 || journal.RecoveryState().ThroughCursor != 0 {
		t.Fatalf("failed write was published: state=%+v", journal.RecoveryState())
	}
	if _, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_HarnessLost{HarnessLost: &eventpb.HarnessLost{}},
	}, AppendOptions{}); !errors.Is(err, ErrJournalFailed) {
		t.Fatalf("writer after storage failure error = %v", err)
	}
}

func TestJournalSealReopensWithDurableReopenEvent(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	journal, err := OpenJournal(ctx, path, "source")
	if err != nil {
		t.Fatal(err)
	}
	first, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_HarnessExited{HarnessExited: &eventpb.HarnessExited{ExitCode: 0}},
	}, AppendOptions{})
	if err != nil {
		t.Fatal(err)
	}
	sealedAt, err := journal.Seal(ctx, "incarnation-a")
	if err != nil || sealedAt != first.GetCursor() {
		t.Fatalf("seal = %d, %v", sealedAt, err)
	}
	if _, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_Native{Native: &eventpb.Native{Line: "after seal"}},
	}, AppendOptions{}); !errors.Is(err, ErrJournalSealed) {
		t.Fatalf("append after seal error = %v", err)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := OpenJournal(ctx, path, "source")
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if cursor, ok := reopened.SealedCursor(); !ok || cursor != first.GetCursor() {
		t.Fatalf("reopened seal = %d, %t", cursor, ok)
	}
	if err := reopened.Reopen(ctx, "incarnation-b"); err != nil {
		t.Fatal(err)
	}
	if _, ok := reopened.SealedCursor(); ok {
		t.Fatal("reopen left the seal published")
	}
	entries, err := reopened.Since(ctx, first.GetCursor(), 1)
	if err != nil || len(entries) != 1 || entries[0].GetEvent().GetJournalReopened().GetIncarnationUid() != "incarnation-b" {
		t.Fatalf("reopen event = %v, %v", entries, err)
	}
}

func TestInitializationLogTruncatesOnlyIncompleteTail(t *testing.T) {
	path := filepath.Join(t.TempDir(), "initialization.jsonl")
	first := &runnerpb.InitializationEvent{
		Sequence: 1, Attempt: 1,
		Observation: &runnerpb.InitializationEvent_Output{Output: &runnerpb.InitializationOutput{
			Stream: runnerpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, Data: []byte("ready"),
		}},
	}
	line, err := (protojson.MarshalOptions{UseProtoNames: true}).Marshal(first)
	if err != nil {
		t.Fatal(err)
	}
	valid := append(append([]byte(nil), line...), '\n')
	data := append(append([]byte(nil), valid...), []byte(`{"sequence":2,"attempt":1,"output":{"stream":"unterminated`)...)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	log, err := OpenInitializationLog(path)
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	if info, err := os.Stat(path); err != nil || info.Size() != int64(len(valid)) {
		t.Fatalf("truncated initialization size = %v, %v", info, err)
	}
	result, err := log.AppendResult(1, 0)
	if err != nil || !result.GetExecuted() {
		t.Fatalf("append result = %v, %v", result, err)
	}
	events := log.Since(0)
	if len(events) != 2 || events[0].GetOutput().GetStream() != runnerpb.InitializationStream_INITIALIZATION_STREAM_STDOUT || events[1].GetSequence() != 2 || !log.Ended() {
		t.Fatalf("initialization events after recovery = %v", events)
	}
}

func TestSessionStorePreservesRunnerSessionJSONShape(t *testing.T) {
	root := t.TempDir()
	store, err := NewSessionStore(root)
	if err != nil {
		t.Fatal(err)
	}
	totalBudget := 32768
	nativeID := "native-session"
	setupHash := "0123456789abcdef"
	record := SessionRecord{
		Harness: runnerpb.Harness_HARNESS_CLAUDE, CWD: "/workspace", Model: "route/model",
		TotalContextBudgetTokens: &totalBudget, ReasoningEffort: "medium", Instructions: "Keep going.",
		SetupScriptSHA256: &setupHash, EventSourceID: "bdb9e654-d77c-42a2-b006-9a0dd4f20655", NativeSessionID: &nativeID,
	}
	if err := store.Write("session-1", record); err != nil {
		t.Fatal(err)
	}
	read, err := store.Read("session-1")
	if err != nil {
		t.Fatal(err)
	}
	if read.Harness != record.Harness || read.Spec().GetCwd() != record.CWD || read.EventSourceID != record.EventSourceID || *read.NativeSessionID != nativeID {
		t.Fatalf("session record did not round trip: %+v", read)
	}
	data, err := os.ReadFile(filepath.Join(root, "session-1", "session.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	if string(fields["harness"]) != `"HARNESS_CLAUDE"` || fields["native_session_id"] == nil || fields["event_source_id"] == nil {
		t.Fatalf("persisted session JSON fields = %s", data)
	}
}

func openTestJournal(t *testing.T) *Journal {
	t.Helper()
	journal, err := OpenJournal(context.Background(), filepath.Join(t.TempDir(), "journal.sqlite"), "test-source")
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

func TestCommittedBatchAllowsReplayAndAdmissionWhileCallbackWaits(t *testing.T) {
	journal := openTestJournal(t)
	committed, release := make(chan struct{}), make(chan struct{})
	done := make(chan error, 1)
	go func() {
		done <- journal.WithBatch(context.Background(), func(ctx context.Context) error {
			if _, err := journal.Append(ctx, &eventpb.Event{Observation: &eventpb.Event_Native{Native: &eventpb.Native{Line: "first"}}}, AppendOptions{}); err != nil {
				return err
			}
			if err := journal.CommitBatch(ctx); err != nil {
				return err
			}
			close(committed)
			<-release
			_, err := journal.Append(ctx, &eventpb.Event{Observation: &eventpb.Event_Native{Native: &eventpb.Native{Line: "last"}}}, AppendOptions{})
			return err
		})
	}()
	<-committed
	replayed := make(chan error, 1)
	go func() {
		entries, err := journal.Since(context.Background(), 0, 10)
		if err != nil {
			replayed <- err
			return
		}
		if len(entries) != 1 || entries[0].GetEvent().GetNative().Line != "first" {
			replayed <- errors.New("committed batch was not available to replay")
			return
		}
		_, _, err = journal.Admit(context.Background(), &commandpb.Command{CommandId: "between", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "between"}}}, "")
		replayed <- err
	}()
	select {
	case err := <-replayed:
		close(release)
		if batchErr := <-done; batchErr != nil {
			t.Fatal(batchErr)
		}
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		close(release)
		<-done
		<-replayed
		t.Fatal("committed callback retained journal writer ownership")
	}
	entries, err := journal.Since(context.Background(), 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 3 || entries[1].GetEvent().GetCommandAdmitted().GetCommand().GetCommandId() != "between" || entries[2].GetEvent().GetNative().Line != "last" {
		t.Fatalf("journal handoff order = %v", entries)
	}
}

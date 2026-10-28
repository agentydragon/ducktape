package journal

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"io/fs"
	"path/filepath"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/pressly/goose/v3"
	"google.golang.org/protobuf/proto"
	"modernc.org/sqlite"
	sqlite3 "modernc.org/sqlite/lib"
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

	journal, err := Open(ctx, path, "stable-source")
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
	if version != 2 {
		t.Fatalf("Goose migration version = %d, want 2", version)
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
	if _, _, err := journal.Admit(ctx, fresh, "turn-1"); !errors.Is(err, ErrSealed) {
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
			OriginCommandIds: []string{"input-a", "input-b"}, Text: "combined", HarnessMessageId: "native-7", TurnId: "turn-1",
		}},
	}
	receipt, err := journal.Append(ctx, event, AppendOptions{
		TerminalCommandIDs: []string{"input-a", "input-b"},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"input-a", "input-b"} {
		command, err := journal.GetCommand(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		if command == nil || command.TerminalCursor == nil || *command.TerminalCursor != receipt.GetCursor() {
			t.Fatalf("receipt for %s = %+v", id, command)
		}
	}
	entries, err := journal.Since(ctx, receipt.GetCursor()-1, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || !proto.Equal(entries[0].GetEvent().GetHarnessUserMessageConfirmed(), event.GetHarnessUserMessageConfirmed()) {
		t.Fatalf("terminal receipt event = %v", entries)
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

func TestNativeRequestSurvivesJournalReopenAndPartialSettlement(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	journal, err := Open(ctx, path, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	commands := []*commandpb.Command{
		{CommandId: "model-1", Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "test-model"}}},
		{CommandId: "effort-1", Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: "high"}}},
		{CommandId: "input-1", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}},
	}
	for _, command := range commands {
		if _, _, err := journal.Admit(ctx, command, ""); err != nil {
			t.Fatal(err)
		}
		if _, err := journal.DispatchPlanned(ctx, command.CommandId); err != nil {
			t.Fatal(err)
		}
	}
	if err := journal.RecordNativeRequest(ctx, "request-1", harness.CodexTurnStart, []string{"input-1", "model-1", "effort-1"}); err != nil {
		t.Fatal(err)
	}
	receipt, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_ModelChanged{ModelChanged: &eventpb.ModelChanged{CommandId: "model-1", Model: "test-model"}},
	}, AppendOptions{TerminalCommandIDs: []string{"model-1"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}
	journal, err = Open(ctx, path, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	defer journal.Close()
	requests, err := journal.PendingNativeRequests(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(requests) != 1 || requests[0].ID != "request-1" || requests[0].Kind != harness.CodexTurnStart || len(requests[0].Commands) != 3 {
		t.Fatalf("recovered requests = %+v", requests)
	}
	for i, record := range requests[0].Commands {
		if !proto.Equal(record.Command, commands[i]) || !record.DispatchPlanned {
			t.Fatalf("recovered command = %+v", record)
		}
	}
	if cursor := requests[0].Commands[0].TerminalCursor; cursor == nil || *cursor != receipt.GetCursor() {
		t.Fatalf("lost settled model receipt: %v", cursor)
	}
	if _, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_ReasoningEffortChanged{ReasoningEffortChanged: &eventpb.ReasoningEffortChanged{CommandId: "effort-1", Effort: "high"}},
	}, AppendOptions{TerminalCommandIDs: []string{"effort-1"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{OriginCommandIds: []string{"input-1"}, HarnessMessageId: "message-1", TurnId: "turn-1", Text: "hello"}},
	}, AppendOptions{TerminalCommandIDs: []string{"input-1"}}); err != nil {
		t.Fatal(err)
	}
	requests, err = journal.PendingNativeRequests(ctx)
	if err != nil || len(requests) != 0 {
		t.Fatalf("settled requests = %+v, error = %v", requests, err)
	}
}

func TestJournalMigratesNativeRequests(t *testing.T) {
	for _, test := range []struct {
		name   string
		legacy string
		kind   harness.NativeRequestKind
	}{
		{name: "empty", legacy: `{}`},
		{
			name:   "receipt",
			legacy: `{"harness_message_id":"message-1","turn_id":"turn-1"}`,
		},
		{
			name: "claude-submit", kind: harness.ClaudeSubmit,
			legacy: `{"claude.submit.message_uuid":"message-1"}`,
		},
		{
			name: "claude-model", kind: harness.ClaudeModelChange,
			legacy: `{"claude.model.request_id":"request-1","claude.model.value":"test-model"}`,
		},
		{
			name: "claude-effort", kind: harness.ClaudeEffortChange,
			legacy: `{"claude.effort.request_id":"request-1","claude.effort.value":"high"}`,
		},
		{
			name: "codex-start", kind: harness.CodexTurnStart,
			legacy: `{"codex.turn_start.request_id":"request-1"}`,
		},
		{
			name: "codex-start-with-changes-and-receipt", kind: harness.CodexTurnStart,
			legacy: `{"codex.turn_start.request_id":"request-1","codex.turn_start.model_command_id":"model-1","codex.turn_start.model_value":"test-model","codex.turn_start.effort_command_id":"effort-1","codex.turn_start.effort_value":"high","harness_message_id":"message-1","turn_id":"turn-1"}`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			ctx := context.Background()
			path := filepath.Join(t.TempDir(), "journal.sqlite")
			db, err := sql.Open("sqlite", path)
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			files, err := fs.Sub(journalMigrationFiles, "migrations")
			if err != nil {
				t.Fatal(err)
			}
			provider, err := goose.NewProvider(goose.DialectSQLite3, db, files,
				goose.WithTableName(journalMigrationTableName), goose.WithDisableGlobalRegistry(true))
			if err != nil {
				t.Fatal(err)
			}
			if _, err := provider.UpTo(ctx, 1); err != nil {
				t.Fatal(err)
			}
			operation := &commandpb.Command{CommandId: "input-legacy", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}}
			if test.kind == harness.ClaudeModelChange {
				operation.Operation = &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "test-model"}}
			}
			if test.kind == harness.ClaudeEffortChange {
				operation.Operation = &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: "high"}}
			}
			commands := []*commandpb.Command{operation}
			if test.name == "codex-start-with-changes-and-receipt" {
				commands = append(commands,
					&commandpb.Command{CommandId: "model-1", Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "test-model"}}},
					&commandpb.Command{CommandId: "effort-1", Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: "high"}}})
			}
			var entryBytes [][]byte
			for i, command := range commands {
				payload, err := proto.Marshal(command)
				if err != nil {
					t.Fatal(err)
				}
				entry := &eventlogpb.EventEntry{
					Cursor: uint64(i + 1), Origin: &eventlogpb.EventOrigin{SourceId: "test-source", Sequence: uint64(i + 1)},
					Event: &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{CommandAdmitted: &eventpb.CommandAdmitted{Command: command}}},
				}
				encoded, err := proto.Marshal(entry)
				if err != nil {
					t.Fatal(err)
				}
				entryBytes = append(entryBytes, encoded)
				if _, err := db.ExecContext(ctx, "INSERT INTO event_entry VALUES (?, ?)", i+1, encoded); err != nil {
					t.Fatal(err)
				}
				correlation := "{}"
				if i == 0 {
					correlation = test.legacy
				}
				if _, err := db.ExecContext(ctx, "INSERT INTO command VALUES (?, ?, ?, 1, ?, NULL)", command.CommandId, payload, i+1, correlation); err != nil {
					t.Fatal(err)
				}
			}
			var terminalCursor *uint64
			if test.name == "receipt" || test.name == "codex-start-with-changes-and-receipt" {
				cursor := uint64(len(entryBytes) + 1)
				terminalCursor = &cursor
				entry := &eventlogpb.EventEntry{
					Cursor: cursor, Origin: &eventlogpb.EventOrigin{SourceId: "test-source", Sequence: cursor},
					Event: &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{OriginCommandIds: []string{operation.CommandId}, HarnessMessageId: "message-1", TurnId: "turn-1", Text: "hello"}}},
				}
				encoded, err := proto.Marshal(entry)
				if err != nil {
					t.Fatal(err)
				}
				entryBytes = append(entryBytes, encoded)
				if _, err := db.ExecContext(ctx, "INSERT INTO event_entry VALUES (?, ?)", cursor, encoded); err != nil {
					t.Fatal(err)
				}
				if _, err := db.ExecContext(ctx, "UPDATE command SET terminal_cursor = ? WHERE command_id = ?", cursor, operation.CommandId); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := db.ExecContext(ctx, "INSERT INTO checkpoint VALUES (1, 'test-source', ?, 0, '')", len(entryBytes)); err != nil {
				t.Fatal(err)
			}
			for range 2 {
				journal, err := Open(ctx, path, "test-source")
				if err != nil {
					t.Fatal(err)
				}
				defer journal.Close()
				requests, err := journal.PendingNativeRequests(ctx)
				if err != nil {
					t.Fatal(err)
				}
				if test.kind == "" {
					if len(requests) != 0 {
						t.Fatalf("unexpected requests: %+v", requests)
					}
				} else {
					if len(requests) != 1 || requests[0].Kind != test.kind || len(requests[0].Commands) != len(commands) {
						t.Fatalf("migrated requests = %+v", requests)
					}
					for i, record := range requests[0].Commands {
						if !proto.Equal(record.Command, commands[i]) || !record.DispatchPlanned {
							t.Fatalf("migrated command = %+v", record)
						}
					}
				}
				mainRecord, err := journal.GetCommand(ctx, operation.CommandId)
				if err != nil {
					t.Fatal(err)
				}
				if mainRecord == nil || (mainRecord.TerminalCursor == nil) != (terminalCursor == nil) {
					t.Fatalf("lost command receipt: %+v", mainRecord)
				}
				if terminalCursor != nil && *mainRecord.TerminalCursor != *terminalCursor {
					t.Fatalf("receipt cursor = %d, want %d", *mainRecord.TerminalCursor, *terminalCursor)
				}
				for i, original := range entryBytes {
					var retained []byte
					if err := journal.conn.QueryRowContext(ctx, "SELECT payload FROM event_entry WHERE cursor = ?", i+1).Scan(&retained); err != nil {
						t.Fatal(err)
					}
					if !bytes.Equal(retained, original) {
						t.Fatal("migration rewrote event bytes")
					}
				}
				if err := journal.Close(); err != nil {
					t.Fatal(err)
				}
			}
		})
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
	}, AppendOptions{}); !errors.Is(err, ErrFailed) {
		t.Fatalf("writer after storage failure error = %v", err)
	}
}

func journalWithExternalReader(t *testing.T) (*Journal, *sql.DB, *sql.Tx) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	journal, err := Open(t.Context(), path, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = journal.Close() })
	reader, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = reader.Close() })
	read, err := reader.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = read.Rollback() })
	var count int
	if err := read.QueryRowContext(t.Context(), "SELECT count(*) FROM event_entry").Scan(&count); err != nil {
		t.Fatal(err)
	}
	return journal, reader, read
}

func TestJournalCommitWaitsForExternalReader(t *testing.T) {
	journal, reader, read := journalWithExternalReader(t)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- journal.WithBatch(ctx, func(ctx context.Context) error {
			_, err := journal.Append(ctx, &eventpb.Event{Observation: &eventpb.Event_HarnessLost{HarnessLost: &eventpb.HarnessLost{}}}, AppendOptions{})
			return err
		})
	}()
	// Once COMMIT holds a PENDING lock, SQLite refuses new readers while it
	// waits for the original reader. Observe that boundary instead of sleeping.
	for {
		select {
		case err := <-done:
			t.Fatalf("commit returned while the external reader held its lock: %v", err)
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		default:
		}
		var count int
		err := reader.QueryRowContext(ctx, "SELECT count(*) FROM event_entry").Scan(&count)
		if err == nil {
			continue
		}
		var sqliteErr *sqlite.Error
		if !errors.As(err, &sqliteErr) || sqliteErr.Code() != sqlite3.SQLITE_BUSY {
			t.Fatalf("probe reader = %v, want lock contention", err)
		}
		break
	}
	if journal.LastCursor() != 0 {
		t.Fatal("blocked commit published its cursor")
	}
	if err := read.Rollback(); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("commit after reader released its lock: %v", err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	if journal.LastCursor() != 1 || journal.RecoveryState().ThroughCursor != 1 {
		t.Fatalf("committed state = %+v, want cursor 1", journal.RecoveryState())
	}
	if _, err := journal.Seal(ctx, "test-incarnation"); err != nil {
		t.Fatalf("seal after transient reader: %v", err)
	}
}

func TestFailedCommitReleasesSQLiteLocks(t *testing.T) {
	for _, batch := range []bool{false, true} {
		name := "append"
		if batch {
			name = "batch"
		}
		t.Run(name, func(t *testing.T) {
			ctx := t.Context()
			journal, reader, read := journalWithExternalReader(t)
			// Force the terminal contention path even when normal writes wait for readers.
			if _, err := journal.conn.ExecContext(ctx, "PRAGMA busy_timeout=0"); err != nil {
				t.Fatal(err)
			}
			var count int
			write := func(ctx context.Context) error {
				_, err := journal.Append(ctx, &eventpb.Event{Observation: &eventpb.Event_HarnessLost{HarnessLost: &eventpb.HarnessLost{}}}, AppendOptions{})
				return err
			}
			var commitErr error
			if batch {
				commitErr = journal.WithBatch(ctx, write)
			} else {
				commitErr = write(ctx)
			}
			if !isStorageError(commitErr) {
				t.Fatalf("commit while external reader holds a lock = %v, want storage error", commitErr)
			}
			if err := read.Rollback(); err != nil {
				t.Fatal(err)
			}
			if err := reader.QueryRowContext(ctx, "SELECT count(*) FROM event_entry").Scan(&count); err != nil {
				t.Fatalf("failed COMMIT retained SQLite locks: %v (commit error: %v)", err, commitErr)
			}
			if count != 0 || journal.LastCursor() != 0 || journal.RecoveryState().ThroughCursor != 0 {
				t.Fatalf("failed commit exposed entries: stored=%d state=%+v", count, journal.RecoveryState())
			}
			if err := write(ctx); !errors.Is(err, ErrFailed) {
				t.Fatalf("writer after failed COMMIT = %v, want failed journal", err)
			}
		})
	}
}

func TestJournalSealReopensWithDurableReopenEvent(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	journal, err := Open(ctx, path, "source")
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
	}, AppendOptions{}); !errors.Is(err, ErrSealed) {
		t.Fatalf("append after seal error = %v", err)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := Open(ctx, path, "source")
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

func openTestJournal(t *testing.T) *Journal {
	t.Helper()
	journal, err := Open(context.Background(), filepath.Join(t.TempDir(), "journal.sqlite"), "test-source")
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

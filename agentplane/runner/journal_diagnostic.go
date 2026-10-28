package runner

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
	_ "modernc.org/sqlite"
)

const (
	journalSnapshotEntryLimit       = 256
	journalSnapshotPayloadLimit     = 64 << 10
	journalSnapshotTurnScanLimit    = 256
	journalSnapshotFailureScanLimit = 32
	journalSnapshotCommandLimit     = 16
)

var (
	safeNativeMethods = map[string]struct{}{
		"account/rateLimits/updated": {},
		"environment/status":         {},
		"initialize":                 {},
		"initialized":                {},
		"item/completed":             {},
		"item/started":               {},
		"thread/resume":              {},
		"thread/start":               {},
		"thread/started":             {},
		"thread/tokenUsage/updated":  {},
		"turn/completed":             {},
		"turn/interrupt":             {},
		"turn/start":                 {},
		"turn/started":               {},
	}
	commandIDPattern = regexp.MustCompile(`(?i)^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$`)
)

// journalDiagnostic is a read-only, bounded view intended for private system-test artifacts. It
// deliberately omits Session instructions and admitted input text while retaining command IDs,
// native-session identity, lifecycle events, and metadata for recent native protocol frames.
type journalDiagnostic struct {
	SessionID              string                       `json:"session_id"`
	EventSourceID          string                       `json:"event_source_id,omitempty"`
	NativeSessionID        *string                      `json:"native_session_id,omitempty"`
	Checkpoint             *journalDiagnosticCheckpoint `json:"checkpoint,omitempty"`
	Seal                   *journalDiagnosticSeal       `json:"seal,omitempty"`
	Entries                []journalDiagnosticEntry     `json:"entries,omitempty"`
	EarliestIndexedFailure *journalDiagnosticEntry      `json:"earliest_indexed_failure,omitempty"`
	FailureScanLimit       bool                         `json:"failure_candidate_scan_limit_reached,omitempty"`
	NamedCommands          []namedCommandDiagnostic     `json:"named_commands,omitempty"`
	SnapshotError          string                       `json:"snapshot_error,omitempty"`
	EntriesTruncated       bool                         `json:"entries_truncated,omitempty"`
}

type journalDiagnosticCheckpoint struct {
	ThroughCursor  uint64 `json:"through_cursor"`
	HarnessRunning bool   `json:"harness_running"`
	ActiveTurnID   string `json:"active_turn_id,omitempty"`
}

type journalDiagnosticSeal struct {
	Cursor         uint64 `json:"cursor"`
	IncarnationUID string `json:"incarnation_uid"`
}

type journalDiagnosticEntry struct {
	Cursor           uint64 `json:"cursor"`
	Kind             string `json:"kind"`
	CommandID        string `json:"command_id,omitempty"`
	Operation        string `json:"operation,omitempty"`
	TurnID           string `json:"turn_id,omitempty"`
	TurnStatus       string `json:"turn_status,omitempty"`
	SetupExitCode    int32  `json:"setup_exit_code,omitempty"`
	HarnessPID       int32  `json:"harness_pid,omitempty"`
	ExitCode         int32  `json:"exit_code,omitempty"`
	StoppedByRunner  bool   `json:"stopped_by_runner,omitempty"`
	NativeDirection  string `json:"native_direction,omitempty"`
	NativeFrameBytes int    `json:"native_frame_bytes,omitempty"`
	NativeMethod     string `json:"native_method,omitempty"`
	NativeRequestID  string `json:"native_request_id,omitempty"`
	NativeErrorKind  string `json:"native_error_kind,omitempty"`
	NativeErrorCode  string `json:"native_error_code,omitempty"`
	PayloadBytes     int    `json:"payload_bytes,omitempty"`
	PayloadOmitted   bool   `json:"payload_omitted,omitempty"`
}

type namedCommandDiagnostic struct {
	CommandID       string  `json:"command_id"`
	Status          string  `json:"status"`
	Operation       string  `json:"operation,omitempty"`
	AdmittedCursor  uint64  `json:"admitted_cursor,omitempty"`
	DispatchPlanned bool    `json:"dispatch_planned,omitempty"`
	AdmissionKind   string  `json:"admission_kind,omitempty"`
	TerminalCursor  *uint64 `json:"terminal_cursor,omitempty"`
	TerminalKind    string  `json:"terminal_kind,omitempty"`
}

type stringListFlag []string

func (values *stringListFlag) String() string { return "" }
func (values *stringListFlag) Set(value string) error {
	if len(*values) >= journalSnapshotCommandLimit {
		return errors.New("too_many_command_ids")
	}
	*values = append(*values, value)
	return nil
}

// JournalSnapshotMain prints one private diagnostic JSON document. A capture problem is data in
// that document rather than a nonzero process exit, so kubectl can still save partial metadata.
func JournalSnapshotMain(args []string) int {
	flags := flag.NewFlagSet("runner journal snapshot", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	stateDir := flags.String("state-dir", "/state", "")
	sessionID := flags.String("session-id", "", "")
	var commandIDs stringListFlag
	flags.Var(&commandIDs, "command-id", "")
	if err := flags.Parse(args); err != nil || flags.NArg() != 0 || *sessionID == "" {
		fmt.Fprintln(os.Stderr, "journal snapshot requires --session-id and accepts no positional arguments")
		return 64
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	snapshot := journalDiagnostic{SessionID: *sessionID}
	if err := captureJournalDiagnostic(ctx, *stateDir, *sessionID, journalSnapshotEntryLimit, commandIDs, &snapshot); err != nil {
		snapshot.SnapshotError = err.Error()
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(snapshot); err != nil {
		fmt.Fprintf(os.Stderr, "encode journal snapshot: %v\n", err)
		return 1
	}
	return 0
}

func captureJournalDiagnostic(
	ctx context.Context, stateDir, sessionID string, limit int, commandIDs []string, result *journalDiagnostic,
) error {
	if _, err := ValidateSessionID(sessionID); err != nil {
		return errors.New("invalid_session_id")
	}
	if limit < 1 {
		return errors.New("invalid_snapshot_limit")
	}
	if limit > journalSnapshotEntryLimit {
		limit = journalSnapshotEntryLimit
	}
	if len(commandIDs) > journalSnapshotCommandLimit {
		return errors.New("too_many_command_ids")
	}
	for _, commandID := range commandIDs {
		if !commandIDPattern.MatchString(commandID) {
			return errors.New("invalid_command_id")
		}
	}
	directory := filepath.Join(stateDir, "sessions", sessionID)
	data, err := os.ReadFile(filepath.Join(directory, "session.json"))
	if err != nil {
		return errors.New("session_metadata_unavailable")
	}
	var record SessionRecord
	if err := json.Unmarshal(data, &record); err != nil {
		return errors.New("session_metadata_invalid")
	}
	if record.EventSourceID == "" {
		return errors.New("session_event_source_missing")
	}
	result.EventSourceID = record.EventSourceID
	result.NativeSessionID = record.NativeSessionID

	path := filepath.Join(directory, "journal.sqlite")
	uri := (&url.URL{Scheme: "file", Path: path}).String() + "?mode=ro"
	db, err := sql.Open("sqlite", uri)
	if err != nil {
		return errors.New("journal_open_failed")
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	conn, err := db.Conn(ctx)
	if err != nil {
		return errors.New("journal_connection_failed")
	}
	defer conn.Close()
	if _, err := conn.ExecContext(ctx, "PRAGMA query_only=ON"); err != nil {
		return errors.New("journal_read_only_setup_failed")
	}
	tx, err := conn.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return errors.New("journal_snapshot_begin_failed")
	}
	defer tx.Rollback()

	var checkpoint journalDiagnosticCheckpoint
	err = tx.QueryRowContext(ctx,
		"SELECT source_id, through_cursor, harness_running, active_turn_id FROM checkpoint WHERE id = 1",
	).Scan(&result.EventSourceID, &checkpoint.ThroughCursor, &checkpoint.HarnessRunning, &checkpoint.ActiveTurnID)
	if err != nil {
		return errors.New("journal_checkpoint_unavailable")
	}
	if result.EventSourceID != record.EventSourceID {
		return errors.New("journal_source_mismatch")
	}
	result.Checkpoint = &checkpoint
	var sealCursor sql.NullInt64
	var sealUID sql.NullString
	err = tx.QueryRowContext(ctx, "SELECT cursor, incarnation_uid FROM seal WHERE id = 1").Scan(&sealCursor, &sealUID)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return errors.New("journal_seal_unavailable")
	}
	if sealCursor.Valid && sealUID.Valid {
		result.Seal = &journalDiagnosticSeal{Cursor: uint64(sealCursor.Int64), IncarnationUID: sealUID.String}
	}

	rows, err := tx.QueryContext(ctx,
		`SELECT cursor, length(payload), CASE WHEN length(payload) <= ? THEN payload ELSE NULL END
		FROM event_entry ORDER BY cursor DESC LIMIT ?`, journalSnapshotPayloadLimit, limit+1)
	if err != nil {
		return errors.New("journal_entries_unavailable")
	}
	type row struct {
		cursor  uint64
		bytes   int
		payload []byte
	}
	var retained []row
	for rows.Next() {
		var cursor int64
		var bytes int
		var payload []byte
		if err := rows.Scan(&cursor, &bytes, &payload); err != nil {
			_ = rows.Close()
			return errors.New("journal_entry_scan_failed")
		}
		if cursor < 1 {
			_ = rows.Close()
			return errors.New("journal_cursor_invalid")
		}
		retained = append(retained, row{cursor: uint64(cursor), bytes: bytes, payload: payload})
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return errors.New("journal_entries_read_failed")
	}
	if err := rows.Close(); err != nil {
		return errors.New("journal_entries_close_failed")
	}
	if len(retained) > limit {
		retained = retained[:limit]
		result.EntriesTruncated = true
	}
	sort.Slice(retained, func(i, j int) bool { return retained[i].cursor < retained[j].cursor })
	firstFailure, scanLimited, err := readEarliestIndexedFailure(ctx, tx, record.EventSourceID)
	if err != nil {
		return errors.New("journal_failure_scan_failed")
	}
	commands, err := readNamedCommandDiagnostics(ctx, tx, record.EventSourceID, commandIDs)
	if err != nil {
		return errors.New("named_command_snapshot_failed")
	}
	if err := tx.Commit(); err != nil {
		return errors.New("journal_snapshot_commit_failed")
	}
	result.Entries = make([]journalDiagnosticEntry, 0, len(retained))
	for _, item := range retained {
		if item.payload == nil {
			result.Entries = append(result.Entries, journalDiagnosticEntry{
				Cursor: item.cursor, Kind: "payload_omitted", PayloadBytes: item.bytes, PayloadOmitted: true,
			})
			continue
		}
		entry := &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(item.payload, entry); err != nil {
			return errors.New("journal_entry_decode_failed")
		}
		if entry.GetCursor() != item.cursor || entry.GetOrigin().GetSourceId() != record.EventSourceID {
			return errors.New("journal_entry_identity_mismatch")
		}
		result.Entries = append(result.Entries, summarizeJournalEntry(entry))
	}
	result.EarliestIndexedFailure = firstFailure
	result.FailureScanLimit = scanLimited
	result.NamedCommands = commands
	return nil
}

// readEarliestIndexedFailure covers the first bounded turn-boundary rows by start_cursor and the
// first bounded set of command terminal cursors. Native protocol errors and other lifecycle
// failures are represented only when they fall inside the recent event tail.
func readEarliestIndexedFailure(ctx context.Context, tx *sql.Tx, sourceID string) (*journalDiagnosticEntry, bool, error) {
	var first *journalDiagnosticEntry
	rows, err := tx.QueryContext(ctx, `
		SELECT end_cursor, turn_id, status FROM turn_boundary ORDER BY start_cursor LIMIT ?`, journalSnapshotTurnScanLimit+1,
	)
	if err != nil {
		return nil, false, err
	}
	turnRows := 0
	scanLimited := false
	for rows.Next() {
		turnRows++
		var endCursor sql.NullInt64
		var turnID string
		var turnStatus sql.NullInt64
		if err := rows.Scan(&endCursor, &turnID, &turnStatus); err != nil {
			_ = rows.Close()
			return nil, false, err
		}
		if turnRows > journalSnapshotTurnScanLimit {
			scanLimited = true
			break
		}
		if !endCursor.Valid || !turnStatus.Valid || eventpb.TurnStatus(turnStatus.Int64) != eventpb.TurnStatus_TURN_STATUS_FAILED {
			continue
		}
		candidate := journalDiagnosticEntry{
			Cursor: uint64(endCursor.Int64), Kind: "turn_completed", TurnID: turnID,
			TurnStatus: eventpb.TurnStatus_name[int32(turnStatus.Int64)],
		}
		if first == nil || candidate.Cursor < first.Cursor {
			copy := candidate
			first = &copy
		}
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return nil, false, err
	}
	if err := rows.Close(); err != nil {
		return nil, false, err
	}

	rows, err = tx.QueryContext(ctx, `
		SELECT c.terminal_cursor, length(e.payload),
		       CASE WHEN length(e.payload) <= ? THEN e.payload ELSE NULL END
		FROM command c JOIN event_entry e ON e.cursor = c.terminal_cursor
		WHERE c.terminal_cursor IS NOT NULL ORDER BY c.terminal_cursor LIMIT ?`,
		journalSnapshotPayloadLimit, journalSnapshotFailureScanLimit+1,
	)
	if err != nil {
		return nil, false, err
	}
	terminalRows := 0
	for rows.Next() {
		terminalRows++
		var cursor int64
		var payloadBytes int
		var payload []byte
		if err := rows.Scan(&cursor, &payloadBytes, &payload); err != nil {
			_ = rows.Close()
			return nil, false, err
		}
		if terminalRows > journalSnapshotFailureScanLimit {
			scanLimited = true
			break
		}
		if payload == nil {
			continue
		}
		entry := &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(payload, entry); err != nil || entry.GetCursor() != uint64(cursor) || entry.GetOrigin().GetSourceId() != sourceID {
			continue
		}
		candidate := summarizeJournalEntry(entry)
		if !isJournalFailure(candidate) || (first != nil && first.Cursor <= candidate.Cursor) {
			continue
		}
		copy := candidate
		first = &copy
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return nil, false, err
	}
	if err := rows.Close(); err != nil {
		return nil, false, err
	}
	return first, scanLimited, nil
}

func readNamedCommandDiagnostics(
	ctx context.Context, tx *sql.Tx, sourceID string, commandIDs []string,
) ([]namedCommandDiagnostic, error) {
	result := make([]namedCommandDiagnostic, 0, len(commandIDs))
	for _, commandID := range commandIDs {
		row := namedCommandDiagnostic{CommandID: commandID, Status: "not_admitted"}
		var payloadBytes int
		var payload []byte
		var admittedCursor int64
		var dispatchPlanned bool
		var terminalCursor sql.NullInt64
		err := tx.QueryRowContext(ctx, `
			SELECT length(payload), CASE WHEN length(payload) <= ? THEN payload ELSE NULL END,
			       admitted_cursor, dispatch_planned, terminal_cursor
			FROM command WHERE command_id = ?`, journalSnapshotPayloadLimit, commandID,
		).Scan(&payloadBytes, &payload, &admittedCursor, &dispatchPlanned, &terminalCursor)
		if errors.Is(err, sql.ErrNoRows) {
			result = append(result, row)
			continue
		}
		if err != nil {
			return nil, err
		}
		row.Status, row.AdmittedCursor, row.DispatchPlanned = "admitted", uint64(admittedCursor), dispatchPlanned
		row.AdmissionKind = "command_admitted"
		if payload == nil {
			row.Operation = "omitted"
		} else {
			command := &commandpb.Command{}
			if err := proto.Unmarshal(payload, command); err != nil || command.GetCommandId() != commandID {
				return nil, errors.New("invalid_command_record")
			}
			row.Operation = commandOperationName(command)
		}
		if terminalCursor.Valid {
			cursor := uint64(terminalCursor.Int64)
			row.TerminalCursor = &cursor
			kind, err := readJournalEventKind(ctx, tx, cursor, sourceID)
			if err != nil {
				return nil, err
			}
			row.TerminalKind = kind
		}
		result = append(result, row)
	}
	return result, nil
}

func readJournalEventKind(ctx context.Context, tx *sql.Tx, cursor uint64, sourceID string) (string, error) {
	var payloadBytes int
	var payload []byte
	if err := tx.QueryRowContext(ctx, `
		SELECT length(payload), CASE WHEN length(payload) <= ? THEN payload ELSE NULL END
		FROM event_entry WHERE cursor = ?`, journalSnapshotPayloadLimit, cursor,
	).Scan(&payloadBytes, &payload); err != nil {
		return "", err
	}
	if payload == nil {
		return "payload_omitted", nil
	}
	entry := &eventlogpb.EventEntry{}
	if err := proto.Unmarshal(payload, entry); err != nil || entry.GetCursor() != cursor || entry.GetOrigin().GetSourceId() != sourceID {
		return "", errors.New("invalid_command_event")
	}
	return summarizeJournalEntry(entry).Kind, nil
}

func isJournalFailure(entry journalDiagnosticEntry) bool {
	switch entry.Kind {
	case "command_failed", "harness_launch_failed", "harness_lost", "setup_interrupted", "native_error":
		return true
	case "turn_completed":
		return entry.TurnStatus == eventpb.TurnStatus_name[int32(eventpb.TurnStatus_TURN_STATUS_FAILED)]
	case "setup_finished":
		return entry.SetupExitCode != 0
	default:
		return false
	}
}

func summarizeJournalEntry(entry *eventlogpb.EventEntry) journalDiagnosticEntry {
	result := journalDiagnosticEntry{Cursor: entry.GetCursor()}
	switch event := entry.GetEvent(); {
	case event.GetCommandAdmitted() != nil:
		command := event.GetCommandAdmitted().GetCommand()
		result.Kind, result.CommandID = "command_admitted", command.GetCommandId()
		result.Operation = commandOperationName(command)
	case event.GetCommandFailed() != nil:
		result.Kind, result.CommandID = "command_failed", event.GetCommandFailed().GetCommandId()
	case event.GetCommandNoop() != nil:
		result.Kind, result.CommandID = "command_noop", event.GetCommandNoop().GetCommandId()
	case event.GetHarnessStarted() != nil:
		result.Kind, result.HarnessPID = "harness_started", event.GetHarnessStarted().GetPid()
	case event.GetHarnessExited() != nil:
		result.Kind = "harness_exited"
		result.ExitCode, result.StoppedByRunner = event.GetHarnessExited().GetExitCode(), event.GetHarnessExited().GetStoppedByRunner()
	case event.GetHarnessLost() != nil:
		result.Kind = "harness_lost"
	case event.GetHarnessLaunchFailed() != nil:
		result.Kind = "harness_launch_failed"
	case event.GetTurnStarted() != nil:
		result.Kind, result.TurnID = "turn_started", event.GetTurnStarted().GetTurnId()
	case event.GetTurnCompleted() != nil:
		result.Kind, result.TurnID = "turn_completed", event.GetTurnCompleted().GetTurnId()
		result.TurnStatus = eventpb.TurnStatus_name[int32(event.GetTurnCompleted().GetStatus())]
	case event.GetNative() != nil:
		result.Kind = "native"
		line := event.GetNative().GetLine()
		result.NativeDirection = eventpb.Direction_name[int32(event.GetNative().GetDirection())]
		result.NativeFrameBytes = len(line)
		var metadata struct {
			Method string          `json:"method"`
			ID     json.RawMessage `json:"id"`
			Error  *struct {
				Code json.RawMessage `json:"code"`
			} `json:"error"`
		}
		if json.Unmarshal([]byte(line), &metadata) == nil {
			if _, ok := safeNativeMethods[metadata.Method]; ok {
				result.NativeMethod = metadata.Method
			}
			result.NativeRequestID = safeJSONScalar(metadata.ID)
			if metadata.Error != nil {
				result.Kind = "native_error"
				result.NativeErrorKind = "json_rpc_error"
				result.NativeErrorCode = safeJSONScalar(metadata.Error.Code)
			}
		}
	default:
		result.Kind = "other"
	}
	return result
}

func commandOperationName(command *commandpb.Command) string {
	if command == nil {
		return "unspecified"
	}
	switch command.GetOperation().(type) {
	case *commandpb.Command_SubmitInput:
		return "submit_input"
	case *commandpb.Command_ChangeModel:
		return "change_model"
	case *commandpb.Command_ChangeReasoningEffort:
		return "change_reasoning_effort"
	case *commandpb.Command_InterruptTurn:
		return "interrupt_turn"
	default:
		return "unspecified"
	}
}

func safeJSONScalar(raw json.RawMessage) string {
	if len(raw) == 0 || len(raw) > 20 {
		return ""
	}
	for index, character := range raw {
		if index == 0 && character == '-' {
			continue
		}
		if character < '0' || character > '9' {
			return ""
		}
	}
	if raw[0] == '-' && len(raw) == 1 {
		return ""
	}
	return string(raw)
}

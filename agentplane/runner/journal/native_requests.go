package journal

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/runner/harness"
)

func (j *Journal) RecordNativeRequest(ctx context.Context, requestID string, kind harness.NativeRequestKind, commandIDs []string) error {
	if requestID == "" || len(commandIDs) == 0 {
		return errors.New("native request requires an ID and commands")
	}
	return j.write(ctx, func(_ *journalBatch) error {
		inputs, models, efforts := 0, 0, 0
		for _, commandID := range commandIDs {
			record, err := j.getCommandLocked(ctx, commandID)
			if err != nil {
				return err
			}
			if record == nil {
				return fmt.Errorf("%w %q", ErrUnknownCommand, commandID)
			}
			if !record.DispatchPlanned || record.TerminalCursor != nil {
				return fmt.Errorf("native request requires a pending dispatched command: %q", commandID)
			}
			switch {
			case record.Command.GetSubmitInput() != nil:
				inputs++
			case record.Command.GetChangeModel() != nil:
				models++
			case record.Command.GetChangeReasoningEffort() != nil:
				efforts++
			default:
				return fmt.Errorf("unsupported native request command %q", commandID)
			}
		}
		valid := false
		switch kind {
		case harness.ClaudeSubmit:
			valid = inputs == 1 && models == 0 && efforts == 0
		case harness.ClaudeModelChange:
			valid = inputs == 0 && models == 1 && efforts == 0
		case harness.ClaudeEffortChange:
			valid = inputs == 0 && models == 0 && efforts == 1
		case harness.CodexTurnStart:
			valid = inputs == 1 && models <= 1 && efforts <= 1
		}
		if !valid {
			return fmt.Errorf("invalid command combination for native request kind %q", kind)
		}
		if _, err := j.conn.ExecContext(ctx, "INSERT INTO native_request (request_id, kind) VALUES (?, ?)", requestID, kind); err != nil {
			return storageError("record native request", err)
		}
		for _, commandID := range commandIDs {
			if _, err := j.conn.ExecContext(
				ctx,
				"INSERT INTO native_request_command (request_id, command_id) VALUES (?, ?)", requestID, commandID,
			); err != nil {
				return storageError("link native request command", err)
			}
		}
		return nil
	})
}

func (j *Journal) PendingCommandRecords(ctx context.Context) ([]harness.CommandRecord, error) {
	if !j.ownsBatch(ctx) {
		j.mu.Lock()
		defer j.mu.Unlock()
	}
	if err := j.checkWritableLocked(); err != nil {
		return nil, err
	}
	rows, err := j.conn.QueryContext(
		ctx,
		"SELECT payload, admitted_cursor, dispatch_planned, terminal_cursor FROM command WHERE terminal_cursor IS NULL ORDER BY admitted_cursor",
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var commands []harness.CommandRecord
	for rows.Next() {
		var payload []byte
		var admitted int64
		var planned bool
		var terminal sql.NullInt64
		if err := rows.Scan(&payload, &admitted, &planned, &terminal); err != nil {
			return nil, err
		}
		record, err := decodeCommandRecord(payload, admitted, planned, terminal)
		if err != nil {
			return nil, err
		}
		commands = append(commands, *record)
	}
	return commands, rows.Err()
}

func (j *Journal) PendingNativeRequests(ctx context.Context) ([]harness.NativeRequest, error) {
	if !j.ownsBatch(ctx) {
		j.mu.Lock()
		defer j.mu.Unlock()
	}
	if err := j.checkWritableLocked(); err != nil {
		return nil, err
	}
	// Include every linked command when any is pending: one response can settle commands separately.
	rows, err := j.conn.QueryContext(ctx, `
		SELECT r.request_id, r.kind, c.payload, c.admitted_cursor, c.dispatch_planned, c.terminal_cursor
		FROM native_request r
		JOIN native_request_command link ON link.request_id = r.request_id
		JOIN command c ON c.command_id = link.command_id
		WHERE EXISTS (
			SELECT 1 FROM native_request_command pending_link
			JOIN command pending ON pending.command_id = pending_link.command_id
			WHERE pending_link.request_id = r.request_id AND pending.terminal_cursor IS NULL
		)
		ORDER BY r.request_id, c.admitted_cursor
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var requests []harness.NativeRequest
	for rows.Next() {
		var id string
		var kind harness.NativeRequestKind
		var payload []byte
		var admitted int64
		var planned bool
		var terminal sql.NullInt64
		if err := rows.Scan(&id, &kind, &payload, &admitted, &planned, &terminal); err != nil {
			return nil, err
		}
		record, err := decodeCommandRecord(payload, admitted, planned, terminal)
		if err != nil {
			return nil, err
		}
		if len(requests) == 0 || requests[len(requests)-1].ID != id {
			requests = append(requests, harness.NativeRequest{ID: id, Kind: kind})
		}
		request := &requests[len(requests)-1]
		request.Commands = append(request.Commands, *record)
	}
	return requests, rows.Err()
}

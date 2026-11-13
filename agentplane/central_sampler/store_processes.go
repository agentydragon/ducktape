package centralsampler

import (
	"context"
	"database/sql"
	"errors"
	"maps"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/outputbuffer"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
)

const processColumns = `id,conversation_id::text,generation,after_seq,status,tty,exit_code,buffered_output,model_session_id,omitted_bytes`

func (s *Store) RegisterProcess(ctx context.Context, owner Owner, processID string, tty bool) error {
	if processID == "" {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO sampler_process(id,conversation_id,generation,after_seq,status,tty,exit_code,buffered_output)
			VALUES($1,$2,$3,NULL,'starting',$4,NULL,''::bytea)`, processID, owner.ConversationID, owner.Generation, tty)
		return s.pgError(err)
	})
}

// RegisterModelProcess records a model-visible process under a unified exec session number. It
// returns ErrConflict while another unreleased process of the conversation holds that number; the
// number is released when its process is recorded as exited.
func (s *Store) RegisterModelProcess(ctx context.Context, owner Owner, processID string, sessionID int, tty bool) error {
	if processID == "" {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO sampler_process(id,conversation_id,generation,after_seq,status,tty,exit_code,buffered_output,model_session_id)
			VALUES($1,$2,$3,NULL,'starting',$4,NULL,''::bytea,$5)`, processID, owner.ConversationID, owner.Generation, tty, sessionID)
		return s.pgError(err)
	})
}

func (s *Store) Process(ctx context.Context, owner Owner, processID string) (*RemoteProcess, error) {
	var process *RemoteProcess
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		var err error
		process, err = scanProcess(tx.QueryRowContext(ctx, `SELECT `+processColumns+`
			FROM sampler_process WHERE id=$1 AND conversation_id=$2`, processID, owner.ConversationID))
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		return err
	})
	return process, err
}

// ProcessByModelSession resolves an unreleased model-facing session number.
func (s *Store) ProcessByModelSession(ctx context.Context, owner Owner, sessionID int) (*RemoteProcess, error) {
	var process *RemoteProcess
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		var err error
		process, err = scanProcess(tx.QueryRowContext(ctx, `SELECT `+processColumns+`
			FROM sampler_process WHERE conversation_id=$1 AND model_session_id=$2 AND status <> 'exited'`, owner.ConversationID, sessionID))
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		return err
	})
	return process, err
}

func scanProcess(row rowScanner) (*RemoteProcess, error) {
	var process RemoteProcess
	var afterSequence sql.NullInt64
	var exitCode sql.NullInt64
	var modelSessionID sql.NullInt64
	var output []byte
	err := row.Scan(&process.ID, &process.ConversationID, &process.Generation, &afterSequence, &process.Status,
		&process.TTY, &exitCode, &output, &modelSessionID, &process.OmittedBytes)
	if err != nil {
		return nil, err
	}
	if modelSessionID.Valid {
		value := int(modelSessionID.Int64)
		process.ModelSessionID = &value
	}
	if afterSequence.Valid {
		value := uint64(afterSequence.Int64)
		process.AfterSequence = &value
	}
	if exitCode.Valid {
		value := int(exitCode.Int64)
		process.ExitCode = &value
	}
	process.BufferedOutput = append([]byte(nil), output...)
	return &process, nil
}

func (s *Store) ProcessResult(ctx context.Context, owner Owner, processID string, afterSequence *uint64, status protocol.ProcessStatus, exitCode *int, consumeOutput bool) error {
	if !status.Valid() {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		process, err := scanProcess(tx.QueryRowContext(ctx, `SELECT `+processColumns+`
			FROM sampler_process WHERE id=$1 AND conversation_id=$2 FOR UPDATE`, processID, owner.ConversationID))
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		if err != nil {
			return err
		}
		// Consuming an observed terminal result is local; it remains readable after takeover.
		if process.Generation != owner.Generation && !(consumeOutput && process.Status == protocol.ProcessExitedUnread && status == protocol.ProcessExited) {
			return &LostOwnershipError{Reason: "process belongs to another owner generation"}
		}
		var sequence any
		if afterSequence != nil {
			sequence = *afterSequence
		}
		var code any
		if exitCode != nil {
			code = *exitCode
		}
		output, omitted := process.BufferedOutput, process.OmittedBytes
		if consumeOutput {
			output, omitted = []byte{}, 0
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_process SET after_seq=$3,status=$4,exit_code=$5,buffered_output=$6,omitted_bytes=$7 WHERE id=$1 AND conversation_id=$2`,
			processID, owner.ConversationID, sequence, status, code, output, omitted); err != nil {
			return err
		}
		process.AfterSequence, process.Status, process.ExitCode = afterSequence, status, exitCode
		if consumeOutput {
			process.BufferedOutput = nil
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		payload := map[string]any{"after_seq": afterSequence, "exit_code": exitCode}
		return s.recordEvidence(ctx, tx, owner, conversation, session, processID, eventpb.ExecutionProgress_KIND_PROCESS, processPhase(status), payload)
	})
}

// AbandonProcess stops scheduling a remote handle whose remote outcome is unknown while
// retaining every observed byte and exit code for inspection. It never records a remote exit.
func (s *Store) AbandonProcess(ctx context.Context, owner Owner, processID string, afterSequence *uint64, output *outputbuffer.Buffer, remoteOutcome NestedOutcome, executorSessionID string) error {
	if remoteOutcome != NestedUnknown && remoteOutcome != NestedNotDispatched {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		process, err := scanProcess(tx.QueryRowContext(ctx, `SELECT `+processColumns+`
			FROM sampler_process WHERE id=$1 AND conversation_id=$2 FOR UPDATE`, processID, owner.ConversationID))
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		if err != nil {
			return err
		}
		if process.Generation != owner.Generation {
			return &LostOwnershipError{Reason: "process belongs to another owner generation"}
		}
		if process.Status == protocol.ProcessAbandoned {
			// Session retirement can abandon this handle while its caller still holds newer
			// observed output. Retain that snapshot without reviving the handle or regressing
			// a cursor already recorded by another settlement.
			if output == nil || process.AfterSequence != nil && (afterSequence == nil || *afterSequence < *process.AfterSequence) {
				return nil
			}
		}
		if process.Status == protocol.ProcessExited {
			return protocol.ErrPrecondition
		}
		if afterSequence == nil {
			afterSequence = process.AfterSequence
		}
		buffered, omitted := process.BufferedOutput, process.OmittedBytes
		if output != nil {
			buffered, omitted = output.RetainedBytes(), output.OmittedBytes()
		}
		var sequence any
		if afterSequence != nil {
			sequence = *afterSequence
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_process SET after_seq=$3,status='abandoned',buffered_output=$4,omitted_bytes=$5 WHERE id=$1 AND conversation_id=$2`,
			processID, owner.ConversationID, sequence, buffered, omitted); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, processID, eventpb.ExecutionProgress_KIND_PROCESS, eventpb.ExecutionProgress_PHASE_ABANDONED, map[string]any{
			"after_seq": afterSequence, "exit_code": process.ExitCode, "remote_outcome": remoteOutcome,
			"automatic_replay": false, "buffered_bytes": len(buffered), "omitted_bytes": omitted,
			"executor_session_id": executorSessionID,
		})
	})
}

func (s *Store) RetainProcessOutput(ctx context.Context, owner Owner, processID string, afterSequence uint64, output []byte, exited bool, exitCode *int) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		process, err := scanProcess(tx.QueryRowContext(ctx, `SELECT `+processColumns+`
			FROM sampler_process WHERE id=$1 AND conversation_id=$2 FOR UPDATE`, processID, owner.ConversationID))
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		if err != nil {
			return err
		}
		if process.Generation != owner.Generation {
			return &LostOwnershipError{Reason: "process belongs to another owner generation"}
		}
		// Unread output keeps its head and tail, as upstream's pending buffer does.
		buffer, err := outputbuffer.Restore(outputbuffer.MaxBytes, process.BufferedOutput, process.OmittedBytes)
		if err != nil {
			return err
		}
		buffer.Append(output)
		buffered := buffer.RetainedBytes()
		status := process.Status
		var code any
		if exitCode != nil {
			code = *exitCode
		}
		if exited {
			status = protocol.ProcessExitedUnread
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_process SET after_seq=$3,status=$4,exit_code=$5,buffered_output=$6,omitted_bytes=$7 WHERE id=$1 AND conversation_id=$2`,
			processID, owner.ConversationID, afterSequence, status, code, buffered, buffer.OmittedBytes()); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		payload := map[string]any{"after_seq": afterSequence, "exit_code": exitCode, "unread_bytes": buffer.TotalBytes()}
		return s.recordEvidence(ctx, tx, owner, conversation, session, processID, eventpb.ExecutionProgress_KIND_PROCESS, processPhase(status), payload)
	})
}

func (s *Store) CellResult(ctx context.Context, owner Owner, cellID string, status protocol.CellStatus) error {
	if cellID == "" || !status.Valid() {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO sampler_cell(id,conversation_id,generation,status) VALUES($1,$2,$3,$4)
			ON CONFLICT(id,conversation_id,generation) DO UPDATE SET status=EXCLUDED.status`, cellID, owner.ConversationID, owner.Generation, status)
		if err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, cellID, eventpb.ExecutionProgress_KIND_CELL, cellPhase(status), map[string]any{})
	})
}

func (s *Store) ExecutionState(ctx context.Context, owner Owner) (*ExecutionState, error) {
	state := &ExecutionState{}
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		processRows, err := tx.QueryContext(ctx, `SELECT `+processColumns+`
			FROM sampler_process WHERE conversation_id=$1 AND status NOT IN ('exited','abandoned')`, owner.ConversationID)
		if err != nil {
			return err
		}
		for processRows.Next() {
			process, err := scanProcess(processRows)
			if err != nil {
				processRows.Close()
				return err
			}
			state.Processes = append(state.Processes, *process)
		}
		if err := processRows.Err(); err != nil {
			processRows.Close()
			return err
		}
		processRows.Close()
		cellRows, err := tx.QueryContext(ctx, `SELECT id,conversation_id::text,generation,status FROM sampler_cell
			WHERE conversation_id=$1 AND status='live'`, owner.ConversationID)
		if err != nil {
			return err
		}
		defer cellRows.Close()
		for cellRows.Next() {
			var cell LiveCell
			if err := cellRows.Scan(&cell.ID, &cell.ConversationID, &cell.Generation, &cell.Status); err != nil {
				return err
			}
			state.Cells = append(state.Cells, cell)
		}
		return cellRows.Err()
	})
	return state, err
}

// LiveWork counts the handles and intents that a successor on another host would block on.
type LiveWork struct {
	LiveCells         int
	LiveProcesses     int
	UnresolvedIntents int
	// Pending reports an unresolved provider, compaction or cell intent pointer.
	Pending bool
}

// KeepsOwner reports whether a draining owner must keep serving: an effect in flight always, a
// live cell or process handle only while the turn that can settle it is active.
func (w LiveWork) KeepsOwner(turnActive bool) bool {
	return w.UnresolvedIntents > 0 || w.Pending || turnActive && (w.LiveCells > 0 || w.LiveProcesses > 0)
}

func (s *Store) LiveWork(ctx context.Context, owner Owner) (LiveWork, error) {
	var work LiveWork
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		work.Pending = conversation.Pending != nil
		return tx.QueryRowContext(ctx, `SELECT
			(SELECT count(*) FROM sampler_cell WHERE conversation_id=$1 AND status='live'),
			(SELECT count(*) FROM sampler_process WHERE conversation_id=$1 AND status NOT IN ('exited','exited_unread','abandoned')),
			(SELECT count(*) FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL)`, conversation.ID).
			Scan(&work.LiveCells, &work.LiveProcesses, &work.UnresolvedIntents)
	})
	return work, err
}

func (s *Store) CheckLiveCells(ctx context.Context, owner Owner, runtimeAvailable bool) error {
	state, err := s.ExecutionState(ctx, owner)
	if err != nil {
		return err
	}
	for _, cell := range state.Cells {
		if !runtimeAvailable || cell.Generation != owner.Generation {
			return &protocol.UnknownEffectError{Reason: "live JavaScript cell lost its host; automatic replay and V8 recovery are unsafe"}
		}
	}
	return nil
}

func (s *Store) RequestInterrupt(ctx context.Context, conversationID string, generation int) (string, error) {
	id := uuid.NewString()
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := scanConversation(tx.QueryRowContext(ctx, `SELECT `+conversationColumns+` FROM sampler_conversation WHERE id=$1 FOR UPDATE`, conversationID))
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		if err != nil {
			return err
		}
		var active bool
		if err := tx.QueryRowContext(ctx, `SELECT owner IS NOT NULL AND generation=$2 AND lease_until>clock_timestamp()
			FROM sampler_conversation WHERE id=$1`, conversationID, generation).Scan(&active); err != nil {
			return err
		}
		if !active || conversation.Owner == nil {
			return &LostOwnershipError{Reason: "interrupt target is not the active owner generation"}
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO sampler_interrupt(id,conversation_id,generation,target_turn,result) VALUES($1,$2,$3,NULL,NULL)`, id, conversationID, generation)
		return err
	})
	return id, err
}

func (s *Store) PendingInterrupts(ctx context.Context, owner Owner) ([]string, error) {
	var ids []string
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		rows, err := tx.QueryContext(ctx, `SELECT id::text FROM sampler_interrupt WHERE conversation_id=$1 AND generation=$2 AND result IS NULL ORDER BY id`, owner.ConversationID, owner.Generation)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id string
			if err := rows.Scan(&id); err != nil {
				return err
			}
			ids = append(ids, id)
		}
		return rows.Err()
	})
	return ids, err
}

func (s *Store) AcknowledgeInterrupt(ctx context.Context, owner Owner, requestIDs []string, result map[string]any) error {
	if len(requestIDs) == 0 {
		return protocol.ErrInvalid
	}
	cleanupComplete, ok := result["cleanup_complete"].(bool)
	if !ok {
		return protocol.ErrInvalid
	}
	resultWire, err := marshal(result)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		for _, requestID := range requestIDs {
			tag, err := tx.ExecContext(ctx, `UPDATE sampler_interrupt SET result=$4 WHERE id=$1 AND conversation_id=$2 AND generation=$3`, requestID, owner.ConversationID, owner.Generation, resultWire)
			if err != nil {
				return err
			}
			rowsAffected, err := tag.RowsAffected()
			if err != nil {
				return err
			}
			if rowsAffected != 1 {
				return protocol.ErrMissing
			}
		}
		activeCall := ""
		if conversation.Pending != nil {
			pendingID := conversation.Pending.ID
			intent, err := intentFor(ctx, tx, pendingID)
			if err != nil {
				return err
			}
			activeCall = intent.CallID
			interrupted := map[string]any{"status": "interrupted", "partial_effects_possible": true}
			maps.Copy(interrupted, result)
			wire, err := marshal(interrupted)
			if err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, pendingID, wire); err != nil {
				return err
			}
			if cleanupComplete {
				conversation.Pending = nil
			}
		} else if !cleanupComplete {
			id := uuid.NewString()
			request := map[string]any{"kind": IntentInterrupt, "requests": requestIDs}
			requestWire, err := marshal(request)
			if err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,$3,$4,$5)`, id, conversation.ID, owner.Generation, requestWire, resultWire); err != nil {
				return err
			}
			conversation.Pending = &PendingIntent{ID: id, Kind: PendingIntentInterrupt}
		}
		if cleanupComplete {
			if _, err := settlePendingCalls(conversation, func(call protocol.ToolInvocation) string {
				if call.CallID == activeCall {
					return "Interrupted by user. Partial effects may have occurred."
				}
				return "Not started because user interrupted the turn."
			}); err != nil {
				return err
			}
			rows, err := tx.QueryContext(ctx, `SELECT id::text FROM sampler_intent WHERE conversation_id=$1 AND generation=$2 AND result IS NULL`, conversation.ID, owner.Generation)
			if err != nil {
				return err
			}
			var unresolved []string
			for rows.Next() {
				var id string
				if err := rows.Scan(&id); err != nil {
					rows.Close()
					return err
				}
				if conversation.Pending != nil && id == conversation.Pending.ID {
					continue
				}
				unresolved = append(unresolved, id)
			}
			rowsErr := rows.Err()
			rows.Close()
			if rowsErr != nil {
				return rowsErr
			}
			for _, id := range unresolved {
				if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result='{"status":"interrupted","partial_effects_possible":true}'::jsonb WHERE id=$1`, id); err != nil {
					return err
				}
			}
		}
		conversation.Stopped = true
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, requestIDs[0], eventpb.ExecutionProgress_KIND_INTERRUPT, eventpb.ExecutionProgress_PHASE_RESULT, result)
	})
}

func (s *Store) AppendInput(ctx context.Context, owner Owner, prompt string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending != nil {
			return &protocol.UnknownEffectError{Reason: "unresolved intent blocks new input; automatic replay is unsafe"}
		}
		if err := conversation.apply(func(state protocol.Context) error {
			return state.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: prompt}})
		}); err != nil {
			return err
		}
		conversation.Stopped = false
		return updateConversation(ctx, tx, conversation)
	})
}

func checkInterrupt(ctx context.Context, tx *sql.Tx, owner Owner) error {
	var id string
	err := tx.QueryRowContext(ctx, `SELECT id::text FROM sampler_interrupt WHERE conversation_id=$1 AND generation=$2 AND result IS NULL LIMIT 1`, owner.ConversationID, owner.Generation).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	return InterruptPendingError{}
}

func hasLiveCell(ctx context.Context, tx *sql.Tx, conversationID string) (bool, error) {
	var found bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_cell WHERE conversation_id=$1 AND status='live')`, conversationID).Scan(&found)
	return found, err
}

func hasLiveProcess(ctx context.Context, tx *sql.Tx, conversationID string) (bool, error) {
	var found bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_process WHERE conversation_id=$1 AND status NOT IN ('exited','exited_unread','abandoned'))`, conversationID).Scan(&found)
	return found, err
}

func (s *Store) interrupts(ctx context.Context, query string, args ...any) ([]map[string]any, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id::text,generation,result FROM sampler_interrupt `+query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []map[string]any
	for rows.Next() {
		var id string
		var generation int
		var raw []byte
		if err := rows.Scan(&id, &generation, &raw); err != nil {
			return nil, err
		}
		payload, err := decodeMap(raw)
		if err != nil {
			return nil, err
		}
		result = append(result, map[string]any{"id": id, "generation": generation, "result": payload})
	}
	return result, rows.Err()
}

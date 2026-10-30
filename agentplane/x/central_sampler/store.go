package centralsampler

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentydragon/ducktape/agentplane/postgres/go"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
)

const (
	OutboxChannel  = "central_sampler_outbox"
	processColumns = `id,conversation_id::text,generation,after_seq,status,tty,exit_code,buffered_output,model_session_id,omitted_bytes`
)

type rowScanner interface {
	Scan(dest ...any) error
}

// Store owns the central sampler's durable conversation, Session and evidence records.
type Store struct {
	db *sql.DB
	// now dates the model-visible environment context.
	now func() time.Time
}

func New(db *sql.DB) *Store { return &Store{db: db, now: time.Now} }

func (s *Store) DB() *sql.DB { return s.db }

func (s *Store) transaction(ctx context.Context, f func(*sql.Tx) error) error {
	return postgres.Transaction(ctx, s.db, sql.TxOptions{}, f)
}

func notify(ctx context.Context, tx *sql.Tx) error {
	return postgres.Notify(ctx, tx, OutboxChannel, "")
}

func marshal(value any) (string, error) {
	encoded, err := json.Marshal(value)
	return string(encoded), err
}

func decodeMap(raw []byte) (map[string]any, error) {
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return nil, nil
	}
	var value map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	return value, nil
}

func decodeItems(raw []byte) ([]ResponsesItem, error) {
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return nil, nil
	}
	var value []ResponsesItem
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil, err
	}
	return value, nil
}

func encodeItems(value []ResponsesItem) (string, error) {
	if value == nil {
		value = []ResponsesItem{}
	}
	encoded, err := json.Marshal(value)
	return string(encoded), err
}

func decodePendingIntent(raw []byte) (*PendingIntent, error) {
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return nil, nil
	}
	var pending PendingIntent
	if err := json.Unmarshal(raw, &pending); err != nil {
		return nil, err
	}
	if err := pending.Validate(); err != nil {
		return nil, err
	}
	return &pending, nil
}

func loadConversationItems(ctx context.Context, tx *sql.Tx, conversation *Conversation) error {
	rows, err := tx.QueryContext(ctx, `SELECT item FROM sampler_conversation_item WHERE conversation_id=$1 ORDER BY ordinal`, conversation.ID)
	if err != nil {
		return err
	}
	items := make([]ResponsesItem, 0)
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			rows.Close()
			return err
		}
		var decoded ResponsesItem
		err := json.Unmarshal(raw, &decoded)
		if err != nil {
			rows.Close()
			return err
		}
		if decoded.Type == "" {
			rows.Close()
			return fmt.Errorf("conversation item %d is not an object", len(items)+1)
		}
		items = append(items, decoded)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	if err := rows.Close(); err != nil {
		return err
	}
	conversation.Items = items
	return nil
}

func nullableString(value *string) any {
	if value == nil {
		return nil
	}
	return *value
}

func parseOwnerToken(owner Owner) (uuid.UUID, error) {
	token, err := uuid.Parse(owner.Token)
	if err != nil {
		return uuid.Nil, fmt.Errorf("owner token must be a UUID: %w", err)
	}
	return token, nil
}

func scanConversation(row rowScanner) (*Conversation, error) {
	var (
		conversation  Conversation
		owner         sql.NullString
		lease         sql.NullTime
		pending       []byte
		executor      sql.NullString
		contextItems  []byte
		instructions  sql.NullString
		contextTokens sql.NullInt64
		shell         sql.NullString
		environment   []byte
	)
	if err := row.Scan(&conversation.ID, &conversation.Generation, &owner, &lease, &pending,
		&conversation.Stopped, &executor, &contextItems, &instructions, &contextTokens, &shell, &environment); err != nil {
		return nil, err
	}
	if shell.Valid {
		conversation.ExecutorShell = &shell.String
	}
	if len(environment) != 0 {
		conversation.EnvironmentContext = &EnvironmentContext{}
		if err := json.Unmarshal(environment, conversation.EnvironmentContext); err != nil {
			return nil, err
		}
	}
	if owner.Valid {
		conversation.Owner = &owner.String
	}
	if lease.Valid {
		conversation.LeaseUntil = &lease.Time
	}
	if executor.Valid {
		conversation.ExecutorSession = &executor.String
	}
	if instructions.Valid {
		conversation.Instructions = &instructions.String
	}
	if contextTokens.Valid {
		value := int(contextTokens.Int64)
		conversation.ContextTokens = &value
	}
	var err error
	if conversation.Pending, err = decodePendingIntent(pending); err != nil {
		return nil, err
	}
	if len(contextItems) != 0 && !bytes.Equal(contextItems, []byte("null")) {
		conversation.ContextItems, err = decodeItems(contextItems)
		if err != nil {
			return nil, err
		}
	}
	return &conversation, nil
}

const conversationColumns = `id::text, generation, owner::text, lease_until, pending, stopped,
	executor_session, context_items, instructions, context_tokens, executor_shell, environment_context`

func lockOwner(ctx context.Context, tx *sql.Tx, owner Owner) (*Conversation, error) {
	token, err := parseOwnerToken(owner)
	if err != nil {
		return nil, err
	}
	row, err := scanConversation(tx.QueryRowContext(ctx,
		`SELECT `+conversationColumns+` FROM sampler_conversation WHERE id=$1 FOR UPDATE`, owner.ConversationID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, &LostOwnershipError{Reason: "conversation no longer exists"}
	}
	if err != nil {
		return nil, err
	}
	var leaseValid bool
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE(generation=$2 AND lease_until > clock_timestamp(),FALSE)
		FROM sampler_conversation WHERE id=$1`, owner.ConversationID, owner.Generation).Scan(&leaseValid); err != nil {
		return nil, err
	}
	if row.Owner == nil || *row.Owner != token.String() || row.Generation != owner.Generation || !leaseValid {
		return nil, &LostOwnershipError{}
	}
	if err := loadConversationItems(ctx, tx, row); err != nil {
		return nil, err
	}
	return row, nil
}

// lockOwnerHeader validates an owner while locking only the small mutable header used by
// evidence appenders. Provider and protocol stream events must not decode the retained transcript.
func lockOwnerHeader(ctx context.Context, tx *sql.Tx, owner Owner) (*Conversation, error) {
	token, err := parseOwnerToken(owner)
	if err != nil {
		return nil, err
	}
	var (
		conversation Conversation
		currentOwner sql.NullString
		lease        sql.NullTime
		pending      []byte
		leaseValid   bool
	)
	err = tx.QueryRowContext(ctx, `SELECT id::text,generation,owner::text,lease_until,pending,
		COALESCE(generation=$2 AND lease_until > clock_timestamp(),FALSE)
		FROM sampler_conversation WHERE id=$1 FOR UPDATE`, owner.ConversationID, owner.Generation).Scan(
		&conversation.ID, &conversation.Generation, &currentOwner, &lease, &pending, &leaseValid,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, &LostOwnershipError{Reason: "conversation no longer exists"}
	}
	if err != nil {
		return nil, err
	}
	if currentOwner.Valid {
		conversation.Owner = &currentOwner.String
	}
	if lease.Valid {
		conversation.LeaseUntil = &lease.Time
	}
	if conversation.Pending, err = decodePendingIntent(pending); err != nil {
		return nil, err
	}
	if !currentOwner.Valid || currentOwner.String != token.String() || !leaseValid {
		return nil, &LostOwnershipError{}
	}
	return &conversation, nil
}

func lockSession(ctx context.Context, tx *sql.Tx, sessionID string) (*Conversation, *Session, error) {
	var conversationID string
	err := tx.QueryRowContext(ctx, `SELECT c.id::text FROM sampler_conversation c
		JOIN sampler_session s ON s.conversation_id=c.id WHERE s.id=$1 FOR UPDATE OF c`, sessionID).Scan(&conversationID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, ErrMissing
	}
	if err != nil {
		return nil, nil, err
	}
	conversation, err := scanConversation(tx.QueryRowContext(ctx, `SELECT `+conversationColumns+` FROM sampler_conversation WHERE id=$1`, conversationID))
	if err != nil {
		return nil, nil, err
	}
	if err := loadConversationItems(ctx, tx, conversation); err != nil {
		return nil, nil, err
	}
	session, err := scanSession(tx.QueryRowContext(ctx, `SELECT id, conversation_id::text, account_uid, configuration, model, effort,
		source_id, cursor, active_turn, active_command, workspace_prepared, setup_state, setup_started, blocked, blocked_reason
		FROM sampler_session WHERE id=$1 FOR UPDATE`, sessionID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, ErrMissing
	}
	return conversation, session, err
}

func scanSession(row rowScanner) (*Session, error) {
	var session Session
	var activeTurn, activeCommand, blockedReason sql.NullString
	var cursor int64
	var setupState int32
	err := row.Scan(&session.ID, &session.ConversationID, &session.AccountUID, &session.Configuration,
		&session.Model, &session.Effort, &session.SourceID, &cursor, &activeTurn, &activeCommand,
		&session.WorkspacePrepared, &setupState, &session.SetupStarted, &session.Blocked, &blockedReason)
	if err != nil {
		return nil, err
	}
	session.Cursor = uint64(cursor)
	session.SetupState = runnerpb.SetupState(setupState)
	if activeTurn.Valid {
		session.ActiveTurn = &activeTurn.String
	}
	if activeCommand.Valid {
		session.ActiveCommand = &activeCommand.String
	}
	if blockedReason.Valid {
		session.BlockedReason = &blockedReason.String
	}
	return &session, nil
}

func appendEvent(ctx context.Context, tx *sql.Tx, session *Session, event *eventpb.Event) (*eventlogpb.EventEntry, error) {
	session.Cursor++
	event.At = timestamppb.Now()
	entry := &eventlogpb.EventEntry{
		Cursor: session.Cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: session.SourceID, Sequence: session.Cursor},
		Event:  event,
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(entry)
	if err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_event(session_id,sequence,entry) VALUES($1,$2,$3)`, session.ID, session.Cursor, wire); err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE sampler_session SET cursor=$2 WHERE id=$1`, session.ID, session.Cursor); err != nil {
		return nil, err
	}
	if err := notify(ctx, tx); err != nil {
		return nil, err
	}
	return entry, nil
}

func (s *Store) OpenSession(ctx context.Context, request *samplerpb.OpenSessionRequest, ownerUID string) (*Session, error) {
	if request == nil || request.GetSessionId() == "" || request.GetSpec() == nil || ownerUID == "" {
		return nil, ErrInvalid
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(request)
	if err != nil {
		return nil, err
	}
	var opened *Session
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, request.SessionId); err != nil {
			return err
		}
		existing, err := scanSession(tx.QueryRowContext(ctx, `SELECT id, conversation_id, account_uid, configuration, model, effort,
			source_id, cursor, active_turn, active_command, workspace_prepared, setup_state, setup_started, blocked, blocked_reason
			FROM sampler_session WHERE id=$1`, request.SessionId))
		if err == nil {
			if existing.AccountUID != ownerUID {
				return ErrForbidden
			}
			previous := &samplerpb.OpenSessionRequest{}
			if err := (proto.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(existing.Configuration, previous); err != nil {
				return err
			}
			if !proto.Equal(previous, request) {
				return ErrConflict
			}
			opened = existing
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		conversationID := uuid.NewString()
		var instructions any
		if request.GetSpec().GetInstructions() != "" {
			instructions = request.GetSpec().GetInstructions()
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_conversation(id,generation,owner,lease_until,pending,stopped,
			executor_session,context_items,instructions,context_tokens) VALUES($1,0,NULL,NULL,NULL,FALSE,NULL,NULL,$2,NULL)`,
			conversationID, instructions); err != nil {
			return err
		}
		setupState := runnerpb.SetupState_SETUP_STATE_NOT_REQUIRED
		if request.GetSetupScript() != "" {
			setupState = runnerpb.SetupState_SETUP_STATE_RUNNING
		}
		opened = &Session{
			ID: request.GetSessionId(), ConversationID: conversationID, AccountUID: ownerUID,
			Configuration: wire, Model: request.GetSpec().GetModel(), Effort: request.GetSpec().GetReasoningEffort(),
			SourceID: uuid.NewString(), SetupState: setupState,
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_session(id,conversation_id,account_uid,configuration,model,effort,
			source_id,cursor,active_turn,active_command,workspace_prepared,setup_state,setup_started,blocked)
			VALUES($1,$2,$3,$4,$5,$6,$7,0,NULL,NULL,FALSE,$8,FALSE,FALSE)`,
			opened.ID, opened.ConversationID, opened.AccountUID, opened.Configuration, opened.Model, opened.Effort, opened.SourceID, int32(setupState)); err != nil {
			return err
		}
		if _, err := appendEvent(ctx, tx, opened, &eventpb.Event{Observation: &eventpb.Event_ExecutionProgress{
			ExecutionProgress: &eventpb.ExecutionProgress{OperationId: opened.ID, Kind: "session", Phase: "opened", PayloadJson: "{}"},
		}}); err != nil {
			return err
		}
		if setupState != runnerpb.SetupState_SETUP_STATE_RUNNING {
			if _, err := appendEvent(ctx, tx, opened, &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}}); err != nil {
				return err
			}
		}
		return nil
	})
	return opened, err
}

func (s *Store) GetSession(ctx context.Context, id, ownerUID string) (*Session, error) {
	session, err := scanSession(s.db.QueryRowContext(ctx, `SELECT id, conversation_id, account_uid, configuration, model, effort, source_id, cursor,
		active_turn, active_command, workspace_prepared, setup_state, setup_started, blocked, blocked_reason FROM sampler_session WHERE id=$1`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrMissing
	}
	if err != nil {
		return nil, err
	}
	if ownerUID != "" && session.AccountUID != ownerUID {
		return nil, ErrForbidden
	}
	return session, nil
}

// CodeSessionEpoch returns the durable logical JavaScript-session generation for a live owner.
func (s *Store) CodeSessionEpoch(ctx context.Context, owner Owner) (int, error) {
	var epoch int
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		return tx.QueryRowContext(ctx, `SELECT code_session_epoch FROM sampler_conversation WHERE id=$1`, owner.ConversationID).Scan(&epoch)
	})
	return epoch, err
}

// RecoverSession abandons one blocked cell/runtime boundary after an operator has inspected the
// recorded evidence. It never retries the provider request or any executor operation.
func (s *Store) RecoverSession(ctx context.Context, id, ownerUID string, expectedCursor uint64, operatorNote, replacementIncarnationUID string) (*Session, error) {
	note := strings.TrimSpace(operatorNote)
	if id == "" || ownerUID == "" || note == "" || len(note) > 4096 || len(replacementIncarnationUID) > 128 || strings.TrimSpace(replacementIncarnationUID) != replacementIncarnationUID {
		return nil, ErrInvalid
	}
	var recovered *Session
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, session, err := lockSession(ctx, tx, id)
		if err != nil {
			return err
		}
		if session.AccountUID != ownerUID {
			return ErrForbidden
		}
		if session.Cursor != expectedCursor || !session.Blocked {
			return ErrPrecondition
		}
		var activeLease bool
		if err := tx.QueryRowContext(ctx, `SELECT owner IS NOT NULL AND lease_until IS NOT NULL AND lease_until>clock_timestamp()
			FROM sampler_conversation WHERE id=$1`, conversation.ID).Scan(&activeLease); err != nil {
			return err
		}
		if activeLease {
			return ErrPrecondition
		}
		if session.SetupState != runnerpb.SetupState_SETUP_STATE_NOT_REQUIRED && session.SetupState != runnerpb.SetupState_SETUP_STATE_SUCCEEDED {
			return ErrPrecondition
		}
		if session.ActiveCommand != nil || session.ActiveTurn != nil {
			return ErrPrecondition
		}
		if conversation.Pending != nil && conversation.Pending.Kind != PendingIntentCell {
			return ErrPrecondition
		}

		type unresolvedIntent struct {
			id   string
			kind string
		}
		rows, err := tx.QueryContext(ctx, `SELECT id::text,request FROM sampler_intent
			WHERE conversation_id=$1 AND result IS NULL ORDER BY id FOR UPDATE`, conversation.ID)
		if err != nil {
			return err
		}
		var unresolved []unresolvedIntent
		pendingIntentFound := conversation.Pending == nil
		for rows.Next() {
			var intentID string
			var requestWire []byte
			if err := rows.Scan(&intentID, &requestWire); err != nil {
				rows.Close()
				return err
			}
			request, err := decodeMap(requestWire)
			if err != nil {
				rows.Close()
				return err
			}
			kind := stringValue(request["kind"])
			if kind != string(PendingIntentCell) && kind != "tool" && kind != "executor" {
				rows.Close()
				return ErrPrecondition
			}
			if kind == string(PendingIntentCell) {
				if conversation.Pending == nil || intentID != conversation.Pending.ID || pendingIntentFound {
					rows.Close()
					return ErrPrecondition
				}
				pendingIntentFound = true
			}
			unresolved = append(unresolved, unresolvedIntent{id: intentID, kind: kind})
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		if err := rows.Close(); err != nil {
			return err
		}
		rejectedCallID, rejectionReason := "", ""
		if !pendingIntentFound {
			// Older input validation recorded a known rejection but retained Pending
			// when other live work existed. Preserve that result during explicit recovery.
			intent, err := intentFor(ctx, tx, conversation.Pending.ID)
			if err != nil {
				return err
			}
			dispatched, known := intent.Result["dispatched"].(bool)
			call, isCall := intent.Request["call"].(map[string]any)
			if intent.ConversationID != conversation.ID || stringValue(intent.Request["kind"]) != "cell" ||
				stringValue(intent.Result["status"]) != "rejected" || !known || dispatched || !isCall {
				return ErrPrecondition
			}
			rejectedCallID = stringValue(call["call_id"])
			rejectionReason = stringValue(intent.Result["reason"])
			if rejectedCallID == "" || rejectionReason == "" {
				return ErrPrecondition
			}
		}

		liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		liveProcess, err := hasLiveProcess(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if len(unresolved) == 0 && !liveCell && !liveProcess && rejectedCallID == "" {
			return ErrPrecondition
		}

		abandonedIntentIDs := make([]string, 0, len(unresolved))
		for _, intent := range unresolved {
			abandonedIntent := map[string]any{"status": "abandoned", "remote_outcome": "unknown", "automatic_replay": false}
			wire, err := marshal(abandonedIntent)
			if err != nil {
				return err
			}
			result, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1 AND result IS NULL`, intent.id, wire)
			if err != nil {
				return err
			}
			updated, err := result.RowsAffected()
			if err != nil {
				return err
			}
			if updated != 1 {
				return ErrPrecondition
			}
			if err := s.recordEvidence(ctx, tx, Owner{ConversationID: conversation.ID, Generation: conversation.Generation}, conversation,
				session, intent.id, intent.kind, "abandoned", abandonedIntent); err != nil {
				return err
			}
			abandonedIntentIDs = append(abandonedIntentIDs, intent.id)
		}

		abortedOutputs := make([]ResponsesItem, 0)
		for _, call := range unansweredCalls(conversation.Items) {
			message := "Aborted by operator recovery; remote effects may have occurred and were not replayed. The continuation uses a new JavaScript session, so prior store() and load() values are unavailable."
			if call.CallID == rejectedCallID {
				message = "Tool call rejected before dispatch: " + rejectionReason + ". No runtime or executor operation was dispatched for this call."
			}
			abortedOutputs = append(abortedOutputs, call.OutputItem([]ResponsesContentPart{{
				Type: "input_text", Text: message,
			}}))
		}
		if len(abortedOutputs) > 0 {
			conversation.Items = append(conversation.Items, abortedOutputs...)
			if conversation.ContextItems != nil {
				conversation.ContextItems = append(conversation.ContextItems, abortedOutputs...)
			}
			for _, output := range abortedOutputs {
				encoded, err := json.Marshal(output.Output)
				if err != nil {
					return err
				}
				if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{
					ItemCompleted: &eventpb.ItemCompleted{ItemId: output.CallID, Outcome: &eventpb.ItemCompleted_Tool{
						Tool: &eventpb.ToolResult{Output: DisplayText(string(encoded)), Succeeded: false},
					}},
				}}); err != nil {
					return err
				}
			}
		}
		resetNotice, err := newResponsesMessage("developer", "An operator recovered this session after reviewing its boundary evidence. Unfinished JavaScript cells and executor operations were abandoned without replay; their remote outcome is unknown. The continuation uses a new JavaScript session, so prior store()/load() values and live-cell handles are unavailable. Recreate any required state in a new exec cell before continuing.")
		if err != nil {
			return err
		}
		conversation.Items = append(conversation.Items, resetNotice)
		if conversation.ContextItems != nil {
			conversation.ContextItems = append(conversation.ContextItems, resetNotice)
		}

		if _, err := tx.ExecContext(ctx, `UPDATE sampler_cell SET status='abandoned' WHERE conversation_id=$1 AND status='live'`, conversation.ID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_process SET status='abandoned'
			WHERE conversation_id=$1 AND status NOT IN ('exited','exited_unread','abandoned')`, conversation.ID); err != nil {
			return err
		}

		previousIncarnationUID := ""
		if replacementIncarnationUID != "" {
			configuration := new(samplerpb.OpenSessionRequest)
			if err := (proto.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(session.Configuration, configuration); err != nil {
				return err
			}
			previousIncarnationUID = configuration.GetExpectedIncarnationUid()
			configuration.ExpectedIncarnationUid = replacementIncarnationUID
			wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(configuration)
			if err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, `UPDATE sampler_session SET configuration=$2 WHERE id=$1`, session.ID, wire); err != nil {
				return err
			}
			session.Configuration = wire
			conversation.ExecutorSession = nil
			conversation.ExecutorShell = nil
		}
		conversation.Pending = nil
		conversation.Stopped = false
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET generation=generation+1,code_session_epoch=code_session_epoch+1,
			owner=NULL,lease_until=NULL WHERE id=$1`, conversation.ID); err != nil {
			return err
		}
		recoveryEvidence := map[string]any{
			"status": "recovered", "operator_note": note, "javascript_state_reset": true,
			"remote_outcome": "unknown", "automatic_replay": false,
			"abandoned_intent_ids": abandonedIntentIDs,
		}
		if replacementIncarnationUID != "" {
			recoveryEvidence["previous_incarnation_uid"] = previousIncarnationUID
			recoveryEvidence["replacement_incarnation_uid"] = replacementIncarnationUID
		}
		if err := s.recordEvidence(ctx, tx, Owner{ConversationID: conversation.ID}, conversation, session,
			uuid.NewString(), "recovery", "committed", recoveryEvidence); err != nil {
			return err
		}
		session.Blocked = false
		session.BlockedReason = nil
		if err := persistSession(ctx, tx, session); err != nil {
			return err
		}
		recovered = session
		return nil
	})
	return recovered, err
}

func (s *Store) ListSessions(ctx context.Context, ownerUID string) ([]*Session, error) {
	query := `SELECT id, conversation_id, account_uid, configuration, model, effort, source_id, cursor,
		active_turn, active_command, workspace_prepared, setup_state, setup_started, blocked, blocked_reason FROM sampler_session`
	args := []any{}
	if ownerUID != "" {
		query += ` WHERE account_uid=$1`
		args = append(args, ownerUID)
	}
	query += ` ORDER BY id`
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var sessions []*Session
	for rows.Next() {
		session, err := scanSession(rows)
		if err != nil {
			return nil, err
		}
		sessions = append(sessions, session)
	}
	return sessions, rows.Err()
}

func Describe(session *Session) (*samplerpb.GetSessionResponse, error) {
	if session == nil {
		return nil, ErrInvalid
	}
	request := &samplerpb.OpenSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, request); err != nil {
		return nil, fmt.Errorf("decode stored session configuration: %w", err)
	}
	spec := proto.Clone(request.GetSpec()).(*runnerpb.SessionSpec)
	spec.Model, spec.ReasoningEffort = session.Model, session.Effort
	state := runnerpb.HarnessState_HARNESS_STATE_RUNNING
	if session.Blocked || session.SetupState == runnerpb.SetupState_SETUP_STATE_RUNNING {
		state = runnerpb.HarnessState_HARNESS_STATE_STOPPED
	}
	outcome := runnerpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED
	if session.SetupState == runnerpb.SetupState_SETUP_STATE_RUNNING {
		outcome = runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING
	} else if session.SetupState == runnerpb.SetupState_SETUP_STATE_FAILED || session.SetupState == runnerpb.SetupState_SETUP_STATE_INTERRUPTED {
		outcome = runnerpb.LaunchOutcome_LAUNCH_OUTCOME_FAILED
	}
	attached := &runnerpb.Attached{
		SessionId: session.ID, Spec: spec, LastCursor: session.Cursor,
		HarnessState: state, SetupState: session.SetupState, LaunchOutcome: outcome,
	}
	if session.ActiveTurn != nil {
		attached.ActiveTurnId = *session.ActiveTurn
	}
	var incarnationUID *string
	if value := request.GetExpectedIncarnationUid(); value != "" {
		incarnationUID = &value
	}
	attached.IncarnationUid = incarnationUID
	blockedReason := ""
	if session.BlockedReason != nil {
		blockedReason = *session.BlockedReason
	}
	return &samplerpb.GetSessionResponse{
		Attached: attached, Executor: request.GetExecutor(),
		ExpectedIncarnationUid: request.GetExpectedIncarnationUid(), Blocked: session.Blocked,
		BlockedReason: blockedReason,
	}, nil
}

func (s *Store) Admit(ctx context.Context, id string, command *commandpb.Command, ownerUID string, validate func(*Session) error) (*eventlogpb.EventEntry, error) {
	if command == nil || command.GetCommandId() == "" || ownerUID == "" {
		return nil, ErrInvalid
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(command)
	if err != nil {
		return nil, err
	}
	var admission *eventlogpb.EventEntry
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, session, err := lockSession(ctx, tx, id)
		if err != nil {
			return err
		}
		if session.AccountUID != ownerUID {
			return ErrForbidden
		}
		var payload []byte
		var sequence uint64
		err = tx.QueryRowContext(ctx, `SELECT payload,admission FROM sampler_command WHERE session_id=$1 AND id=$2`, id, command.CommandId).Scan(&payload, &sequence)
		if err == nil {
			if !bytes.Equal(payload, wire) {
				return ErrConflict
			}
			var entryBytes []byte
			if err := tx.QueryRowContext(ctx, `SELECT entry FROM sampler_event WHERE session_id=$1 AND sequence=$2`, id, sequence).Scan(&entryBytes); err != nil {
				return err
			}
			admission = &eventlogpb.EventEntry{}
			return proto.Unmarshal(entryBytes, admission)
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if session.Blocked {
			return ErrPrecondition
		}
		if validate != nil {
			// Validate against the state after earlier commands, under the admission lock.
			projected := *session
			rows, err := tx.QueryContext(ctx, `SELECT payload FROM sampler_command
                WHERE session_id=$1 AND outcome=$2 ORDER BY admission`, id,
				int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
			if err != nil {
				return err
			}
			defer rows.Close()
			for rows.Next() {
				var wire []byte
				if err := rows.Scan(&wire); err != nil {
					return err
				}
				queued := &commandpb.Command{}
				if err := proto.Unmarshal(wire, queued); err != nil {
					return err
				}
				if model := queued.GetChangeModel(); model != nil {
					projected.Model = model.GetModel()
				}
				if effort := queued.GetChangeReasoningEffort(); effort != nil {
					projected.Effort = effort.GetEffort()
				}
			}
			if err := rows.Err(); err != nil {
				return err
			}
			rows.Close()
			if err := validate(&projected); err != nil {
				return err
			}
		}
		if interrupt := command.GetInterruptTurn(); interrupt != nil {
			if session.ActiveTurn == nil || *session.ActiveTurn != interrupt.GetTurnId() || conversation.Owner == nil {
				return ErrPrecondition
			}
			if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_interrupt(id,conversation_id,generation,target_turn,result)
				VALUES($1,$2,$3,$4,NULL)`, uuid.New(), conversation.ID, conversation.Generation, interrupt.GetTurnId()); err != nil {
				return err
			}
		}
		admission, err = appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{
			CommandAdmitted: &eventpb.CommandAdmitted{Command: command},
		}})
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO sampler_command(session_id,id,payload,admission,outcome,failure_reason)
			VALUES($1,$2,$3,$4,$5,NULL)`, id, command.CommandId, wire, admission.Cursor,
			int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
		return err
	})
	return admission, err
}

func (s *Store) GetCommand(ctx context.Context, id, commandID string) (*samplerpb.GetCommandResponse, error) {
	var wire []byte
	var outcome int32
	var failure sql.NullString
	var sequence uint64
	if err := s.db.QueryRowContext(ctx, `SELECT admission,outcome,failure_reason FROM sampler_command WHERE session_id=$1 AND id=$2`, id, commandID).Scan(&sequence, &outcome, &failure); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrMissing
		}
		return nil, err
	}
	if err := s.db.QueryRowContext(ctx, `SELECT entry FROM sampler_event WHERE session_id=$1 AND sequence=$2`, id, sequence).Scan(&wire); err != nil {
		return nil, err
	}
	entry := &eventlogpb.EventEntry{}
	if err := proto.Unmarshal(wire, entry); err != nil {
		return nil, err
	}
	response := &samplerpb.GetCommandResponse{Admission: entry, Outcome: samplerpb.CommandOutcome(outcome)}
	if failure.Valid {
		response.FailureReason = failure.String
	}
	return response, nil
}

func (s *Store) Events(ctx context.Context, id string, after uint64, limit int) ([]*eventlogpb.EventEntry, error) {
	if limit <= 0 || limit > 4096 {
		return nil, ErrInvalid
	}
	rows, err := s.db.QueryContext(ctx, `SELECT entry FROM sampler_event WHERE session_id=$1 AND sequence>$2 ORDER BY sequence LIMIT $3`, id, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var entries []*eventlogpb.EventEntry
	for rows.Next() {
		var wire []byte
		if err := rows.Scan(&wire); err != nil {
			return nil, err
		}
		entry := &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(wire, entry); err != nil {
			return nil, err
		}
		entries = append(entries, entry)
	}
	return entries, rows.Err()
}

func (s *Store) Checkpoint(ctx context.Context, id, target string) (*uint64, error) {
	var cursor int64
	err := s.db.QueryRowContext(ctx, `SELECT cursor FROM sampler_export_checkpoint WHERE session_id=$1 AND destination=$2`, id, target).Scan(&cursor)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	value := uint64(cursor)
	return &value, nil
}

func (s *Store) CommitCheckpoint(ctx context.Context, id, target string, cursor uint64) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, _, err := lockSession(ctx, tx, id); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO sampler_export_checkpoint(session_id,destination,cursor) VALUES($1,$2,$3)
			ON CONFLICT(session_id,destination) DO UPDATE SET cursor=GREATEST(sampler_export_checkpoint.cursor,EXCLUDED.cursor)`, id, target, cursor)
		return err
	})
}

func (s *Store) PendingSessions(ctx context.Context, target string, exclude []string, after string, limit int) ([]*Session, error) {
	if limit <= 0 || limit > 4096 {
		return nil, ErrInvalid
	}
	if exclude == nil {
		exclude = []string{}
	}
	rows, err := s.db.QueryContext(ctx, `SELECT ss.id,ss.conversation_id::text,ss.account_uid,ss.configuration,ss.model,ss.effort,ss.source_id,ss.cursor,
		ss.active_turn,ss.active_command,ss.workspace_prepared,ss.setup_state,ss.setup_started,ss.blocked,ss.blocked_reason
		FROM sampler_session ss LEFT JOIN sampler_export_checkpoint cp ON cp.session_id=ss.id AND cp.destination=$1
		WHERE NOT (ss.id=ANY($3::varchar[])) AND (cp.cursor IS NULL OR cp.cursor<ss.cursor)
		ORDER BY CASE WHEN ss.id>$2 THEN 0 ELSE 1 END, ss.id LIMIT $4`, target, after, pq.Array(exclude), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var sessions []*Session
	for rows.Next() {
		session, err := scanSession(rows)
		if err != nil {
			return nil, err
		}
		sessions = append(sessions, session)
	}
	return sessions, rows.Err()
}

func (s *Store) RecoverableSessions(ctx context.Context, exclude []string, limit int) ([]string, error) {
	if limit <= 0 {
		return nil, ErrInvalid
	}
	if exclude == nil {
		exclude = []string{}
	}
	rows, err := s.db.QueryContext(ctx, `SELECT ss.id FROM sampler_session ss JOIN sampler_conversation c ON c.id=ss.conversation_id
		WHERE ss.blocked=FALSE AND (c.owner IS NULL OR c.lease_until IS NULL OR c.lease_until<=clock_timestamp())
		AND NOT (ss.id=ANY($1::varchar[])) AND (
		ss.setup_state=$2 OR EXISTS(SELECT 1 FROM sampler_command cmd WHERE cmd.session_id=ss.id AND cmd.outcome=$3)
		OR EXISTS(SELECT 1 FROM sampler_cell cell WHERE cell.conversation_id=c.id AND cell.status='live')
		OR EXISTS(SELECT 1 FROM sampler_process proc WHERE proc.conversation_id=c.id AND proc.status NOT IN ('exited','exited_unread','abandoned')))
		ORDER BY ss.id LIMIT $4`, pq.Array(exclude), int32(runnerpb.SetupState_SETUP_STATE_RUNNING),
		int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func (s *Store) Claim(ctx context.Context, conversationID string, lease time.Duration) (Owner, error) {
	if lease <= 0 {
		return Owner{}, ErrInvalid
	}
	var owner Owner
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := scanConversation(tx.QueryRowContext(ctx, `SELECT `+conversationColumns+` FROM sampler_conversation WHERE id=$1 FOR UPDATE`, conversationID))
		if errors.Is(err, sql.ErrNoRows) {
			return ErrMissing
		}
		if err != nil {
			return err
		}
		var active bool
		if err := tx.QueryRowContext(ctx, `SELECT owner IS NOT NULL AND lease_until IS NOT NULL AND lease_until>clock_timestamp()
			FROM sampler_conversation WHERE id=$1`, conversationID).Scan(&active); err != nil {
			return err
		}
		if active {
			return &LostOwnershipError{Reason: "conversation already has a live sampler"}
		}
		token := uuid.New()
		conversation.Generation++
		micros := lease.Microseconds()
		if micros < 1 {
			micros = 1
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET generation=$2,owner=$3,
			lease_until=clock_timestamp()+($4::bigint * interval '1 microsecond') WHERE id=$1`, conversationID, conversation.Generation, token, micros); err != nil {
			return err
		}
		owner = Owner{ConversationID: conversationID, Generation: conversation.Generation, Token: token.String()}
		return nil
	})
	return owner, err
}

func (s *Store) Renew(ctx context.Context, owner Owner, lease time.Duration) error {
	if lease <= 0 {
		return ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		micros := lease.Microseconds()
		if micros < 1 {
			micros = 1
		}
		_, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET lease_until=clock_timestamp()+($4::bigint * interval '1 microsecond') WHERE id=$1 AND owner=$2 AND generation=$3`,
			owner.ConversationID, owner.Token, owner.Generation, micros)
		return err
	})
}

func (s *Store) Release(ctx context.Context, owner Owner) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET owner=NULL,lease_until=NULL WHERE id=$1`, owner.ConversationID); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `UPDATE sampler_interrupt SET result=$3 WHERE conversation_id=$1 AND generation=$2 AND result IS NULL`,
			owner.ConversationID, owner.Generation, `{"status":"not_applied","reason":"owner completed before observing request"}`)
		return err
	})
}

func (s *Store) Read(ctx context.Context, owner Owner) (*Conversation, error) {
	var result *Conversation
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var err error
		result, err = lockOwner(ctx, tx, owner)
		return err
	})
	return result, err
}

func (s *Store) Create(ctx context.Context, prompt string, instructions *string) (string, error) {
	if prompt == "" {
		return "", ErrInvalid
	}
	id := uuid.NewString()
	item, err := newResponsesMessage("user", prompt)
	if err != nil {
		return "", err
	}
	items := []ResponsesItem{item}
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_conversation(id,generation,owner,lease_until,pending,stopped,
			executor_session,context_items,instructions,context_tokens) VALUES($1,0,NULL,NULL,NULL,FALSE,NULL,NULL,$2,NULL)`, id, nullableString(instructions)); err != nil {
			return err
		}
		return appendConversationItems(ctx, tx, &Conversation{ID: id, Items: items})
	})
	return id, err
}

func (s *Store) Status(ctx context.Context, conversationID string) (map[string]any, error) {
	conversation, err := scanConversation(s.db.QueryRowContext(ctx, `SELECT `+conversationColumns+` FROM sampler_conversation WHERE id=$1`, conversationID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrMissing
	}
	if err != nil {
		return nil, err
	}
	interrupts, err := s.interrupts(ctx, `WHERE conversation_id=$1 ORDER BY generation,id`, conversationID)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"generation": conversation.Generation, "owner": conversation.Owner,
		"pending": conversation.Pending, "stopped": conversation.Stopped, "interrupts": interrupts,
	}, nil
}

func (s *Store) ProviderMetadata(ctx context.Context, owner Owner) (SessionAttribution, error) {
	var sessionID string
	var activeTurn sql.NullString
	if err := s.db.QueryRowContext(ctx, `SELECT id,active_turn FROM sampler_session WHERE conversation_id=$1`, owner.ConversationID).Scan(&sessionID, &activeTurn); err != nil {
		return SessionAttribution{}, err
	}
	turn := sessionID
	if activeTurn.Valid {
		turn = activeTurn.String
	}
	return SessionAttribution{SessionID: sessionID, ThreadID: sessionID, TurnID: turn}, nil
}

func (s *Store) ProviderHeaders(ctx context.Context, owner Owner) (map[string]string, error) {
	metadata, err := s.ProviderMetadata(ctx, owner)
	if err != nil {
		return nil, err
	}
	return map[string]string{"x-agentplane-session-id": metadata.SessionID}, nil
}

func (s *Store) pgError(err error) error {
	var pgErr *pq.Error
	if errors.As(err, &pgErr) && string(pgErr.Code) == "23505" {
		return ErrConflict
	}
	return err
}

func updateConversation(ctx context.Context, tx *sql.Tx, conversation *Conversation) error {
	if err := appendConversationItems(ctx, tx, conversation); err != nil {
		return err
	}
	var contextItems any
	var err error
	if conversation.ContextItems != nil {
		contextItems, err = encodeItems(conversation.ContextItems)
		if err != nil {
			return err
		}
	}
	pending, err := marshal(conversation.Pending)
	if err != nil {
		return err
	}
	var contextTokens any
	if conversation.ContextTokens != nil {
		contextTokens = *conversation.ContextTokens
	}
	environment, err := marshal(conversation.EnvironmentContext)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE sampler_conversation SET pending=$2,stopped=$3,executor_session=$4,
		context_items=$5,context_tokens=$6,executor_shell=$7,environment_context=$8 WHERE id=$1`, conversation.ID,
		nullableJSON(pending), conversation.Stopped, nullableString(conversation.ExecutorSession), contextItems, contextTokens,
		nullableString(conversation.ExecutorShell), nullableJSON(environment))
	return err
}

func appendConversationItems(ctx context.Context, tx *sql.Tx, conversation *Conversation) error {
	var stored int
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(ordinal),0) FROM sampler_conversation_item WHERE conversation_id=$1`, conversation.ID).Scan(&stored); err != nil {
		return err
	}
	if stored > len(conversation.Items) {
		return fmt.Errorf("conversation item store has %d items, memory has %d", stored, len(conversation.Items))
	}
	for index := stored; index < len(conversation.Items); index++ {
		item, err := json.Marshal(conversation.Items[index])
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_conversation_item(conversation_id,ordinal,item) VALUES($1,$2,$3)`,
			conversation.ID, index+1, item); err != nil {
			return err
		}
	}
	return nil
}

func nullableJSON(value string) any {
	if value == "" || value == "null" {
		return nil
	}
	return value
}

func (s *Store) Begin(ctx context.Context, owner Owner, request map[string]any) (string, error) {
	kind := PendingIntentKind(stringValue(request["kind"]))
	if !kind.valid() {
		return "", ErrInvalid
	}
	id := uuid.NewString()
	requestWire, err := marshal(request)
	if err != nil {
		return "", err
	}
	var conversation *Conversation
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		var err error
		conversation, err = lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if err := checkInterrupt(ctx, tx, owner); err != nil {
			return err
		}
		if conversation.Pending != nil {
			return &UnknownEffectError{Reason: fmt.Sprintf("unresolved %s intent %s; automatic replay is unsafe", conversation.Pending.Kind, conversation.Pending.ID)}
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,$3,$4,NULL)`,
			id, owner.ConversationID, owner.Generation, requestWire); err != nil {
			return err
		}
		conversation.Pending = &PendingIntent{ID: id, Kind: kind}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, id, string(kind), "dispatch", request)
	})
	return id, err
}

func (s *Store) Finish(ctx context.Context, owner Owner, intentID string, result map[string]any, items []ResponsesItem, contextTokens *int) error {
	resultWire, err := marshal(result)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if !pendingMatches(conversation.Pending, intentID, PendingIntentKind("")) {
			return ErrPrecondition
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if intent.ConversationID != owner.ConversationID || intent.Generation != owner.Generation {
			return &LostOwnershipError{Reason: "intent belongs to another owner generation"}
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, resultWire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := s.recordEvidence(ctx, tx, owner, conversation, session, intentID, stringValue(intent.Request["kind"]), "result", result); err != nil {
			return err
		}
		conversation.Items = append(conversation.Items, items...)
		if conversation.ContextItems != nil {
			conversation.ContextItems = append(conversation.ContextItems, items...)
		}
		if intent.Request["kind"] == "sampling" {
			conversation.ContextTokens = contextTokens
		}
		conversation.Pending = nil
		return updateConversation(ctx, tx, conversation)
	})
}

func (s *Store) Reject(ctx context.Context, owner Owner, intentID string, failure *KnownFailureError) error {
	if failure == nil {
		return ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if !pendingMatches(conversation.Pending, intentID, PendingIntentKind("")) {
			return ErrPrecondition
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		unresolved, err := hasUnresolvedIntent(ctx, tx, conversation.ID, intentID)
		if err != nil {
			return err
		}
		liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		liveProcess, err := hasLiveProcess(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if unresolved || liveCell || liveProcess {
			result := map[string]any{"status": "rejected", "not_dispatched": []any{}}
			for key, value := range failure.Evidence {
				result[key] = value
			}
			wire, err := marshal(result)
			if err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, wire); err != nil {
				return err
			}
			conversation.Stopped = true
			if err := updateConversation(ctx, tx, conversation); err != nil {
				return err
			}
			session, err := sessionForConversation(ctx, tx, conversation.ID)
			if err != nil {
				return err
			}
			if err := s.recordEvidence(ctx, tx, owner, conversation, session, intentID, stringValue(intent.Request["kind"]), "rejected", result); err != nil {
				return err
			}
			return s.settleFailure(ctx, tx, owner, conversation, session, session.ActiveCommand, failure)
		}
		outputs := notDispatchedOutputs(conversation.Items)
		result := map[string]any{"status": "rejected", "not_dispatched": outputs}
		for key, value := range failure.Evidence {
			result[key] = value
		}
		wire, err := marshal(result)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, wire); err != nil {
			return err
		}
		conversation.Items = append(conversation.Items, outputs...)
		if conversation.ContextItems != nil {
			conversation.ContextItems = append(conversation.ContextItems, outputs...)
		}
		conversation.Stopped = true
		conversation.Pending = nil
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := s.recordEvidence(ctx, tx, owner, conversation, session, intentID, stringValue(intent.Request["kind"]), "rejected", result); err != nil {
			return err
		}
		return s.settleFailure(ctx, tx, owner, conversation, session, session.ActiveCommand, failure)
	})
}

func (s *Store) BeginCompaction(ctx context.Context, owner Owner, request map[string]any, original []ResponsesItem) (string, error) {
	id := uuid.NewString()
	requestWire, err := marshal(request)
	if err != nil {
		return "", err
	}
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending != nil {
			return &UnknownEffectError{Reason: "unresolved intent; cannot compact"}
		}
		if err := checkInterrupt(ctx, tx, owner); err != nil {
			return err
		}
		if !sameItems(conversation.Context(), original) {
			return ErrPrecondition
		}
		liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if liveCell {
			return &UnknownEffectError{Reason: "cannot compact over live JavaScript cells"}
		}
		unresolved, err := hasUnresolvedIntent(ctx, tx, conversation.ID, "")
		if err != nil {
			return err
		}
		if unresolved {
			return &UnknownEffectError{Reason: "unresolved nested intent; cannot compact"}
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,$3,$4,NULL)`, id, owner.ConversationID, owner.Generation, requestWire); err != nil {
			return err
		}
		conversation.Pending = &PendingIntent{ID: id, Kind: PendingIntentCompaction}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, id, "compaction", "dispatch", request)
	})
	return id, err
}

func (s *Store) FinishCompaction(ctx context.Context, owner Owner, intentID string, audit map[string]any, original, replacement []ResponsesItem) error {
	resultWire, err := marshal(audit)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if !pendingMatches(conversation.Pending, intentID, PendingIntentCompaction) {
			return ErrPrecondition
		}
		if !sameItems(conversation.Context(), original) {
			return ErrPrecondition
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, resultWire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		sources, err := s.recordEvidenceWithSources(ctx, tx, owner, conversation, session, intentID, stringValue(intent.Request["kind"]), "result", audit)
		if err != nil {
			return err
		}
		// Codex rust-v0.162.1 core/src/compact.rs insert_initial_context_before_last_real_user_or_summary:
		// with no retained user message, the full context goes before the compaction item.
		environment, err := s.environmentContext(conversation, session)
		if err != nil {
			return err
		}
		environmentItem, err := newResponsesMessage("user", environmentContextUpdate(nil, environment))
		if err != nil {
			return err
		}
		if len(replacement) > 0 && replacement[0].Type == ResponsesItemAnthropicMessage {
			conversation.ContextItems = replacement
		} else {
			conversation.ContextItems = append([]ResponsesItem{environmentItem}, replacement...)
		}
		conversation.EnvironmentContext = &environment
		conversation.ContextTokens = nil
		conversation.Pending = nil
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return s.completeCompactionCommand(ctx, tx, session, sources)
	})
}

func (s *Store) completeCompactionCommand(ctx context.Context, tx *sql.Tx, session *Session, sources []uint64) error {
	if session.ActiveCommand == nil {
		return nil
	}
	command, err := commandFor(ctx, tx, session.ID, *session.ActiveCommand)
	if errors.Is(err, ErrMissing) {
		return nil
	}
	if err != nil {
		return err
	}
	if command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
		return nil
	}
	payload := &commandpb.Command{}
	if err := proto.Unmarshal(command.Payload, payload); err != nil {
		return err
	}
	if payload.GetCompact() == nil {
		return nil
	}
	if err := setCommandOutcome(ctx, tx, command.SessionID, command.ID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED, nil); err != nil {
		return err
	}
	session.ActiveCommand = nil
	if _, err := appendEvent(ctx, tx, session, &eventpb.Event{
		SourceSequences: sources,
		Observation: &eventpb.Event_ExecutionProgress{ExecutionProgress: &eventpb.ExecutionProgress{
			OperationId: command.ID, Kind: "command", Phase: "completed", PayloadJson: "{}",
		}},
	}); err != nil {
		return err
	}
	return persistSession(ctx, tx, session)
}

func (s *Store) RecordNested(ctx context.Context, owner Owner, request map[string]any) (string, error) {
	id := uuid.NewString()
	kind := stringValue(request["kind"])
	if kind == "" {
		return "", ErrInvalid
	}
	requestWire, err := marshal(request)
	if err != nil {
		return "", err
	}
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if kind == "tool" {
			if err := checkInterrupt(ctx, tx, owner); err != nil {
				return err
			}
		}
		if pendingMatches(conversation.Pending, "", PendingIntentCompaction) {
			return &UnknownEffectError{Reason: "cannot start a nested effect during compaction"}
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,$3,$4,NULL)`, id, owner.ConversationID, owner.Generation, requestWire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, id, kind, "dispatch", request)
	})
	return id, err
}

func (s *Store) FinishNested(ctx context.Context, owner Owner, intentID string, result map[string]any) error {
	wire, err := marshal(result)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if intent.Generation != owner.Generation || intent.ConversationID != owner.ConversationID {
			return &LostOwnershipError{Reason: "nested intent belongs to another owner generation"}
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, wire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, intentID, stringValue(intent.Request["kind"]), "result", result)
	})
}

func (s *Store) RegisterProcess(ctx context.Context, owner Owner, processID string, tty bool) error {
	if processID == "" {
		return ErrInvalid
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
		return ErrInvalid
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

// SetExecutorSession records the exec-server session and the shell it reported, if any.
func (s *Store) SetExecutorSession(ctx context.Context, owner Owner, sessionID string, shell *string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.ExecutorSession != nil && *conversation.ExecutorSession != sessionID {
			liveProcess, err := hasLiveProcess(ctx, tx, conversation.ID)
			if err != nil {
				return err
			}
			liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
			if err != nil {
				return err
			}
			if liveProcess || liveCell {
				return &UnknownEffectError{Reason: "executor returned another session; existing handles cannot be replaced"}
			}
		}
		conversation.ExecutorSession = &sessionID
		conversation.ExecutorShell = shell
		return updateConversation(ctx, tx, conversation)
	})
}

// RecordEnvironmentContext appends an environment update before a sampling step, as Codex does
// before each step, and returns the updated conversation.
func (s *Store) RecordEnvironmentContext(ctx context.Context, owner Owner) (*Conversation, error) {
	var conversation *Conversation
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var err error
		conversation, err = lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending != nil {
			return &UnknownEffectError{Reason: "unresolved intent blocks an environment update; automatic replay is unsafe"}
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		previous := conversation.EnvironmentContext
		if err := s.recordEnvironmentContext(conversation, session); err != nil {
			return err
		}
		if previous != nil && previous.equal(*conversation.EnvironmentContext) {
			return nil
		}
		return updateConversation(ctx, tx, conversation)
	})
	return conversation, err
}

func (s *Store) environmentContext(conversation *Conversation, session *Session) (EnvironmentContext, error) {
	request := &samplerpb.OpenSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, request); err != nil {
		return EnvironmentContext{}, err
	}
	return currentEnvironmentContext(request.GetSpec().GetCwd(), conversation.ExecutorShell, s.now()), nil
}

// recordEnvironmentContext appends the update from the conversation's baseline to the Session's
// current environment, then advances the baseline. The caller persists the conversation.
func (s *Store) recordEnvironmentContext(conversation *Conversation, session *Session) error {
	current, err := s.environmentContext(conversation, session)
	if err != nil {
		return err
	}
	if update := environmentContextUpdate(conversation.EnvironmentContext, current); update != "" {
		item, err := newResponsesMessage("user", update)
		if err != nil {
			return err
		}
		conversation.Items = append(conversation.Items, item)
		if conversation.ContextItems != nil {
			conversation.ContextItems = append(conversation.ContextItems, item)
		}
	}
	conversation.EnvironmentContext = &current
	return nil
}

func persistSession(ctx context.Context, tx *sql.Tx, session *Session) error {
	_, err := tx.ExecContext(ctx, `UPDATE sampler_session SET model=$2,effort=$3,active_turn=$4,active_command=$5,
		workspace_prepared=$6,setup_state=$7,setup_started=$8,blocked=$9,blocked_reason=$10,cursor=$11 WHERE id=$1`,
		session.ID, session.Model, session.Effort, nullableString(session.ActiveTurn), nullableString(session.ActiveCommand),
		session.WorkspacePrepared, int32(session.SetupState), session.SetupStarted, session.Blocked, nullableString(session.BlockedReason), session.Cursor)
	return err
}

func (s *Store) PreparedWorkspace(ctx context.Context, owner Owner, sessionID string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		session.WorkspacePrepared = true
		if err := appendProgress(ctx, tx, session, sessionID, "workspace", "prepared", "{}"); err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
}

func (s *Store) StartSetup(ctx context.Context, owner Owner, sessionID string) (bool, error) {
	started := false
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if session.SetupStarted {
			return s.settleSetup(ctx, tx, conversation, session, nil)
		}
		session.SetupStarted = true
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_SetupStarted{SetupStarted: &eventpb.SetupStarted{}}}); err != nil {
			return err
		}
		started = true
		return persistSession(ctx, tx, session)
	})
	return started, err
}

func (s *Store) SetupOutput(ctx context.Context, owner Owner, sessionID, stream string, data []byte) error {
	if stream != "stdout" && stream != "stderr" {
		return ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		output := &eventpb.SetupOutput{}
		if stream == "stdout" {
			output.Stream = &eventpb.SetupOutput_Stdout{Stdout: append([]byte(nil), data...)}
		} else {
			output.Stream = &eventpb.SetupOutput_Stderr{Stderr: append([]byte(nil), data...)}
		}
		_, err = appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_SetupOutput{SetupOutput: output}})
		if err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
}

func (s *Store) FinishSetup(ctx context.Context, owner Owner, sessionID string, exitCode *int) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		return s.settleSetup(ctx, tx, conversation, session, exitCode)
	})
}

func (s *Store) settleSetup(ctx context.Context, tx *sql.Tx, conversation *Conversation, session *Session, exitCode *int) error {
	if session.SetupState != runnerpb.SetupState_SETUP_STATE_RUNNING {
		return ErrPrecondition
	}
	session.Blocked = false
	session.BlockedReason = nil
	if exitCode == nil {
		session.SetupState = runnerpb.SetupState_SETUP_STATE_INTERRUPTED
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_SetupInterrupted{SetupInterrupted: &eventpb.SetupInterrupted{}}}); err != nil {
			return err
		}
	} else {
		if *exitCode == 0 {
			session.SetupState = runnerpb.SetupState_SETUP_STATE_SUCCEEDED
		} else {
			session.SetupState = runnerpb.SetupState_SETUP_STATE_FAILED
		}
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_SetupFinished{
			SetupFinished: &eventpb.SetupFinished{ExitCode: int32(*exitCode)},
		}}); err != nil {
			return err
		}
	}
	if exitCode != nil && *exitCode == 0 {
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}}); err != nil {
			return err
		}
	} else {
		session.Blocked = true
		var reason string
		if exitCode == nil {
			reason = "setup was interrupted; inspect retained setup output and executor state before opening a new Session because setup attempts are never replayed"
		} else {
			reason = fmt.Sprintf("setup exited with status %d; inspect retained setup output, correct the setup, and open a new Session because setup attempts are never replayed", *exitCode)
		}
		session.BlockedReason = &reason
		conversation.Stopped = true
		if err := s.blockQueue(ctx, tx, session, reason); err != nil {
			return err
		}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
	}
	return persistSession(ctx, tx, session)
}

func appendProgress(ctx context.Context, tx *sql.Tx, session *Session, operationID, kind, phase, payload string) error {
	_, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_ExecutionProgress{
		ExecutionProgress: &eventpb.ExecutionProgress{OperationId: operationID, Kind: kind, Phase: phase, PayloadJson: payload},
	}})
	return err
}

func (s *Store) NextCommand(ctx context.Context, owner Owner, sessionID string) (*Command, error) {
	var next *Command
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if session.Blocked {
			return ErrPrecondition
		}
		if session.ActiveCommand != nil {
			if err := s.ensureTurnInterrupt(ctx, tx, owner, session); err != nil {
				return err
			}
			next, err = commandFor(ctx, tx, sessionID, *session.ActiveCommand)
			return err
		}
		rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command
			WHERE session_id=$1 AND outcome=$2 ORDER BY admission LIMIT 1 FOR UPDATE`, sessionID,
			int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
		if err != nil {
			return err
		}
		if rows.Next() {
			next, err = scanCommand(rows)
			rows.Close()
			if err != nil {
				return err
			}
		} else {
			rows.Close()
			return rows.Err()
		}
		payload := &commandpb.Command{}
		if err := proto.Unmarshal(next.Payload, payload); err != nil {
			return err
		}
		switch {
		case payload.GetSubmitInput() != nil:
			if conversation.Pending != nil {
				return &UnknownEffectError{Reason: "unresolved intent blocks queued input; automatic replay is unsafe"}
			}
			// Codex records environment updates before the turn's user input.
			if err := s.recordEnvironmentContext(conversation, session); err != nil {
				return err
			}
			text := payload.GetSubmitInput().GetText()
			item, err := newResponsesMessage("user", text)
			if err != nil {
				return err
			}
			conversation.Items = append(conversation.Items, item)
			if conversation.ContextItems != nil {
				conversation.ContextItems = append(conversation.ContextItems, item)
			}
			conversation.Stopped = false
			session.ActiveTurn = &next.ID
			session.ActiveCommand = &next.ID
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{
				HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
					HarnessMessageId: next.ID,
					Text:             text, OriginCommandIds: []string{next.ID}, TurnId: next.ID,
				},
			}}); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{
				TurnStarted: &eventpb.TurnStarted{TurnId: next.ID, Model: session.Model},
			}}); err != nil {
				return err
			}
		case payload.GetChangeModel() != nil:
			previous := session.Model
			session.Model = payload.GetChangeModel().GetModel()
			next.Outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED
			if err := setCommandOutcome(ctx, tx, sessionID, next.ID, next.Outcome, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_ModelChanged{
				ModelChanged: &eventpb.ModelChanged{CommandId: next.ID, PreviousModel: previous, Model: session.Model},
			}}); err != nil {
				return err
			}
		case payload.GetChangeReasoningEffort() != nil:
			previous := session.Effort
			session.Effort = payload.GetChangeReasoningEffort().GetEffort()
			marker := ResponsesItem{Type: ResponsesItemEffort, Input: session.Effort}
			conversation.Items = append(conversation.Items, marker)
			if conversation.ContextItems != nil {
				conversation.ContextItems = append(conversation.ContextItems, marker)
			}
			next.Outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED
			if err := setCommandOutcome(ctx, tx, sessionID, next.ID, next.Outcome, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_ReasoningEffortChanged{
				ReasoningEffortChanged: &eventpb.ReasoningEffortChanged{CommandId: next.ID, PreviousEffort: previous, Effort: session.Effort},
			}}); err != nil {
				return err
			}
		case payload.GetInterruptTurn() != nil:
			next.Outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED
			if err := setCommandOutcome(ctx, tx, sessionID, next.ID, next.Outcome, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandNoop{
				CommandNoop: &eventpb.CommandNoop{CommandId: next.ID, Reason: "target turn has ended"},
			}}); err != nil {
				return err
			}
		case payload.GetCompact() != nil:
			session.ActiveCommand = &next.ID
		default:
			return ErrInvalid
		}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
	return next, err
}

// steeringInputs returns the queued SubmitInput commands at the head of the session's queue, in
// admission order, that can join its active input turn. Other queued commands keep their order: input
// admitted after one waits for a later turn. A pending interrupt of the turn takes precedence.
func steeringInputs(ctx context.Context, tx *sql.Tx, conversation *Conversation, session *Session) ([]*Command, []*commandpb.SubmitInput, error) {
	if session.ActiveTurn == nil || session.ActiveCommand == nil || *session.ActiveCommand != *session.ActiveTurn {
		return nil, nil, nil
	}
	var interrupting bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_interrupt WHERE conversation_id=$1 AND generation=$2 AND target_turn=$3 AND result IS NULL)`,
		conversation.ID, conversation.Generation, *session.ActiveTurn).Scan(&interrupting); err != nil {
		return nil, nil, err
	}
	if interrupting {
		return nil, nil, nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command
		WHERE session_id=$1 AND outcome=$2 ORDER BY admission FOR UPDATE`, session.ID,
		int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	var commands []*Command
	var inputs []*commandpb.SubmitInput
	for rows.Next() {
		command, err := scanCommand(rows)
		if err != nil {
			return nil, nil, err
		}
		if command.ID == *session.ActiveCommand {
			continue
		}
		payload := &commandpb.Command{}
		if err := proto.Unmarshal(command.Payload, payload); err != nil {
			return nil, nil, err
		}
		if payload.GetSubmitInput() == nil {
			break
		}
		commands = append(commands, command)
		inputs = append(inputs, payload.GetSubmitInput())
	}
	return commands, inputs, rows.Err()
}

// HasSteeringInput reports whether queued user input can join the session's active turn. A running
// cell's unresolved intent does not hide it: that is when a foreground observation should yield.
func (s *Store) HasSteeringInput(ctx context.Context, owner Owner, sessionID string) (bool, error) {
	found := false
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		commands, _, err := steeringInputs(ctx, tx, conversation, session)
		found = len(commands) > 0
		return err
	})
	return found, err
}

// DrainSteeringInput records queued user input into the active turn and applies its commands.
//
// Codex rust-v0.162.1 core/src/session/turn.rs run_turn: input submitted while a turn runs is
// pending input, recorded at the start of the next sampling step; pending input also keeps a turn
// that would otherwise end sampling.
func (s *Store) DrainSteeringInput(ctx context.Context, owner Owner, sessionID string) (bool, error) {
	drained := false
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		// Input joins only at a step boundary, never between an intent and its outcome.
		if conversation.Pending != nil {
			return nil
		}
		commands, inputs, err := steeringInputs(ctx, tx, conversation, session)
		if err != nil || len(commands) == 0 {
			return err
		}
		for index, command := range commands {
			text := inputs[index].GetText()
			item, err := newResponsesMessage("user", text)
			if err != nil {
				return err
			}
			conversation.Items = append(conversation.Items, item)
			if conversation.ContextItems != nil {
				conversation.ContextItems = append(conversation.ContextItems, item)
			}
			if err := setCommandOutcome(ctx, tx, sessionID, command.ID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{
				HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
					HarnessMessageId: command.ID,
					Text:             text, OriginCommandIds: []string{command.ID}, TurnId: *session.ActiveTurn,
				},
			}}); err != nil {
				return err
			}
		}
		drained = true
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
	return drained, err
}

func scanCommand(row rowScanner) (*Command, error) {
	var command Command
	var outcome int32
	var failure sql.NullString
	if err := row.Scan(&command.SessionID, &command.ID, &command.Payload, &command.Admission, &outcome, &failure); err != nil {
		return nil, err
	}
	command.Outcome = samplerpb.CommandOutcome(outcome)
	if failure.Valid {
		command.FailureReason = &failure.String
	}
	return &command, nil
}

func (s *Store) ensureTurnInterrupt(ctx context.Context, tx *sql.Tx, owner Owner, session *Session) error {
	if session.ActiveTurn == nil {
		return nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT payload FROM sampler_command WHERE session_id=$1 AND outcome=$2 ORDER BY admission`,
		session.ID, int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
	if err != nil {
		return err
	}
	found := false
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			rows.Close()
			return err
		}
		candidate := &commandpb.Command{}
		if err := proto.Unmarshal(raw, candidate); err != nil {
			rows.Close()
			return err
		}
		interrupt := candidate.GetInterruptTurn()
		if interrupt == nil || interrupt.GetTurnId() != *session.ActiveTurn {
			continue
		}
		found = true
		break
	}
	rowsErr := rows.Err()
	rows.Close()
	if rowsErr != nil {
		return rowsErr
	}
	if !found {
		return nil
	}
	var pending bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_interrupt WHERE conversation_id=$1 AND generation=$2 AND target_turn=$3 AND result IS NULL)`,
		owner.ConversationID, owner.Generation, *session.ActiveTurn).Scan(&pending); err != nil {
		return err
	}
	if pending {
		return nil
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO sampler_interrupt(id,conversation_id,generation,target_turn,result) VALUES($1,$2,$3,$4,NULL)`,
		uuid.New(), owner.ConversationID, owner.Generation, *session.ActiveTurn)
	return err
}

func (s *Store) FinishCommand(ctx context.Context, owner Owner, sessionID, commandID, result string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		command, err := commandFor(ctx, tx, sessionID, commandID)
		if err != nil {
			return err
		}
		if command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
			return nil
		}
		session.ActiveCommand = nil
		// Codex records pending turn input when a turn finishes and clears it when the turn aborts.
		if _, err := s.settleNotifications(ctx, tx, owner, conversation, session, result == "done"); err != nil {
			return err
		}
		outcome := samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED
		if result == "effects_unknown" {
			outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN
		}
		if err := setCommandOutcome(ctx, tx, sessionID, commandID, outcome, nil); err != nil {
			return err
		}
		session.Blocked = result == "effects_unknown"
		session.BlockedReason = nil
		if session.Blocked {
			reason, err := blockedReason(ctx, tx, conversation, "the command ended with an unknown effect outcome")
			if err != nil {
				return err
			}
			session.BlockedReason = &reason
		}
		if session.ActiveTurn != nil && *session.ActiveTurn == commandID {
			if err := settleTurnInterrupts(ctx, tx, session, result); err != nil {
				return err
			}
			rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command
				WHERE session_id=$1 AND outcome=$2 ORDER BY admission FOR UPDATE`, sessionID,
				int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
			if err != nil {
				return err
			}
			interruptID := ""
			var interrupts []*Command
			for rows.Next() {
				candidate, err := scanCommand(rows)
				if err != nil {
					rows.Close()
					return err
				}
				payload := &commandpb.Command{}
				if err := proto.Unmarshal(candidate.Payload, payload); err != nil {
					rows.Close()
					return err
				}
				interrupt := payload.GetInterruptTurn()
				if interrupt == nil || interrupt.GetTurnId() != commandID {
					continue
				}
				interrupts = append(interrupts, candidate)
			}
			if err := rows.Err(); err != nil {
				rows.Close()
				return err
			}
			rows.Close()
			for _, candidate := range interrupts {
				if err := setCommandOutcome(ctx, tx, sessionID, candidate.ID, outcome, nil); err != nil {
					return err
				}
				if result == "done" {
					if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandNoop{
						CommandNoop: &eventpb.CommandNoop{CommandId: candidate.ID, Reason: "turn completed before interrupt was applied"},
					}}); err != nil {
						return err
					}
				} else {
					interruptID = candidate.ID
				}
			}
			turnStatus := eventpb.TurnStatus_TURN_STATUS_INTERRUPTED
			if result == "done" {
				turnStatus = eventpb.TurnStatus_TURN_STATUS_COMPLETED
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{
				TurnCompleted: &eventpb.TurnCompleted{TurnId: commandID, Status: turnStatus, InterruptedByCommandId: interruptID},
			}}); err != nil {
				return err
			}
			session.ActiveTurn = nil
		} else if err := appendProgress(ctx, tx, session, commandID, "command", "completed", "{}"); err != nil {
			return err
		}
		if session.Blocked {
			if err := s.blockQueue(ctx, tx, session, *session.BlockedReason); err != nil {
				return err
			}
		}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
}

func settleTurnInterrupts(ctx context.Context, tx *sql.Tx, session *Session, result string) error {
	if session.ActiveTurn == nil {
		return nil
	}
	wire, err := marshal(map[string]any{"status": "not_applied", "reason": "target turn ended", "turn_result": result})
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE sampler_interrupt SET result=$3 WHERE conversation_id=$1 AND target_turn=$2 AND result IS NULL`, session.ConversationID, *session.ActiveTurn, wire)
	return err
}

func (s *Store) blockQueue(ctx context.Context, tx *sql.Tx, session *Session, reason string) error {
	rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command
		WHERE session_id=$1 AND outcome=$2 ORDER BY admission FOR UPDATE`, session.ID,
		int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
	if err != nil {
		return err
	}
	var queued []*Command
	for rows.Next() {
		command, err := scanCommand(rows)
		if err != nil {
			rows.Close()
			return err
		}
		queued = append(queued, command)
	}
	rowsErr := rows.Err()
	rows.Close()
	if rowsErr != nil {
		return rowsErr
	}
	for _, command := range queued {
		payload := &commandpb.Command{}
		if err := proto.Unmarshal(command.Payload, payload); err != nil {
			return err
		}
		outcome := samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED
		if payload.GetInterruptTurn() != nil {
			outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN
		}
		if err := setCommandOutcome(ctx, tx, command.SessionID, command.ID, outcome, &reason); err != nil {
			return err
		}
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandFailed{
			CommandFailed: &eventpb.CommandFailed{CommandId: command.ID, Reason: reason},
		}}); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) FailExecution(ctx context.Context, owner Owner, sessionID string, commandID *string, failure error) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		return s.settleFailure(ctx, tx, owner, conversation, session, commandID, failure)
	})
}

func blockedReason(ctx context.Context, tx *sql.Tx, conversation *Conversation, summary string) (string, error) {
	cause := "the execution ended before its external effects could be confirmed"
	if conversation.Pending != nil {
		cause = fmt.Sprintf("%s intent %s has an unresolved outcome", conversation.Pending.Kind, conversation.Pending.ID)
	} else {
		var intentID, kind sql.NullString
		err := tx.QueryRowContext(ctx, `SELECT id::text,request->>'kind' FROM sampler_intent
			WHERE conversation_id=$1 AND result IS NULL ORDER BY id LIMIT 1`, conversation.ID).Scan(&intentID, &kind)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return "", err
		}
		if intentID.Valid {
			cause = fmt.Sprintf("%s intent %s has an unresolved outcome", kind.String, intentID.String)
		} else {
			var cellID string
			err := tx.QueryRowContext(ctx, `SELECT id FROM sampler_cell WHERE conversation_id=$1 AND status='live' ORDER BY id LIMIT 1`, conversation.ID).Scan(&cellID)
			if err != nil && !errors.Is(err, sql.ErrNoRows) {
				return "", err
			}
			if err == nil {
				cause = fmt.Sprintf("JavaScript cell %s has no live host", cellID)
			} else {
				var processID string
				err := tx.QueryRowContext(ctx, `SELECT id FROM sampler_process WHERE conversation_id=$1 AND status NOT IN ('exited','exited_unread','abandoned') ORDER BY id LIMIT 1`, conversation.ID).Scan(&processID)
				if err != nil && !errors.Is(err, sql.ErrNoRows) {
					return "", err
				}
				if err == nil {
					cause = fmt.Sprintf("executor process %s has no confirmed terminal result", processID)
				}
			}
		}
	}
	return fmt.Sprintf("%s; session is blocked because %s. Inspect the linked boundary evidence and reconcile external effects before manual recovery. Automatic effect replay is disabled.", summary, cause), nil
}

func (s *Store) settleFailure(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation, session *Session,
	commandID *string, failure error,
) error {
	var command *Command
	confirmedInput := false
	if commandID != nil {
		var err error
		command, err = commandFor(ctx, tx, session.ID, *commandID)
		if err != nil && !errors.Is(err, ErrMissing) {
			return err
		}
		if command != nil && command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
			return nil
		}
		if command != nil && session.ActiveTurn != nil && *session.ActiveTurn == command.ID {
			payload := &commandpb.Command{}
			if err := proto.Unmarshal(command.Payload, payload); err != nil {
				return err
			}
			confirmedInput = payload.GetSubmitInput() != nil
		}
	}
	unresolved, err := hasUnresolvedIntent(ctx, tx, conversation.ID, "")
	if err != nil {
		return err
	}
	liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
	if err != nil {
		return err
	}
	liveProcess, err := hasLiveProcess(ctx, tx, conversation.ID)
	if err != nil {
		return err
	}
	var knownFailure *KnownFailureError
	known := errors.As(failure, &knownFailure) && knownFailure != nil
	session.Blocked = !known || conversation.Pending != nil || unresolved || liveCell || liveProcess
	session.ActiveCommand = nil
	// Deviation: Codex records pending input after a failed turn; a failed turn here may still hold
	// an unresolved intent whose output must precede it, so queued notify() outputs are discarded.
	if _, err := s.settleNotifications(ctx, tx, owner, conversation, session, false); err != nil {
		return err
	}
	reason := "execution stopped; inspect durable boundary evidence"
	if failure != nil {
		reason = DisplayText(failure.Error())
	}
	session.BlockedReason = nil
	if session.Blocked {
		reason, err = blockedReason(ctx, tx, conversation, reason)
		if err != nil {
			return err
		}
		session.BlockedReason = &reason
	}
	if command != nil {
		outcome := samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED
		if session.Blocked {
			outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN
		}
		if err := setCommandOutcome(ctx, tx, command.SessionID, command.ID, outcome, &reason); err != nil {
			return err
		}
		// Confirmation already settles this command in History; retain only the sampler's run outcome.
		if !confirmedInput {
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandFailed{
				CommandFailed: &eventpb.CommandFailed{CommandId: command.ID, Reason: reason},
			}}); err != nil {
				return err
			}
		}
	}
	conversation.Stopped = true
	payload, err := json.Marshal(map[string]string{"reason": reason})
	if err != nil {
		return err
	}
	phase := "failed"
	if session.Blocked {
		phase = "blocked"
	}
	if err := appendProgress(ctx, tx, session, session.ID, "session", phase, string(payload)); err != nil {
		return err
	}
	if session.ActiveTurn != nil {
		if err := settleTurnInterrupts(ctx, tx, session, "failed"); err != nil {
			return err
		}
		rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command WHERE session_id=$1 AND outcome=$2 ORDER BY admission FOR UPDATE`,
			session.ID, int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
		if err != nil {
			return err
		}
		var interrupts []*Command
		for rows.Next() {
			candidate, err := scanCommand(rows)
			if err != nil {
				rows.Close()
				return err
			}
			payload := &commandpb.Command{}
			if err := proto.Unmarshal(candidate.Payload, payload); err != nil {
				rows.Close()
				return err
			}
			interrupt := payload.GetInterruptTurn()
			if interrupt == nil || interrupt.GetTurnId() != *session.ActiveTurn {
				continue
			}
			interrupts = append(interrupts, candidate)
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		for _, candidate := range interrupts {
			if err := setCommandOutcome(ctx, tx, session.ID, candidate.ID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandNoop{
				CommandNoop: &eventpb.CommandNoop{CommandId: candidate.ID, Reason: "target turn failed"},
			}}); err != nil {
				return err
			}
		}
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{
			TurnCompleted: &eventpb.TurnCompleted{TurnId: *session.ActiveTurn, Status: eventpb.TurnStatus_TURN_STATUS_FAILED, Error: reason},
		}}); err != nil {
			return err
		}
		session.ActiveTurn = nil
	}
	if session.Blocked {
		if err := s.blockQueue(ctx, tx, session, "session blocked by unresolved effects"); err != nil {
			return err
		}
	}
	if err := updateConversation(ctx, tx, conversation); err != nil {
		return err
	}
	return persistSession(ctx, tx, session)
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
			return ErrMissing
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
			return ErrMissing
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

func (s *Store) ProcessResult(ctx context.Context, owner Owner, processID string, afterSequence *uint64, status string, exitCode *int, consumeOutput bool) error {
	if status == "" {
		return ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		process, err := scanProcess(tx.QueryRowContext(ctx, `SELECT `+processColumns+`
			FROM sampler_process WHERE id=$1 AND conversation_id=$2 FOR UPDATE`, processID, owner.ConversationID))
		if errors.Is(err, sql.ErrNoRows) {
			return ErrMissing
		}
		if err != nil {
			return err
		}
		// Consuming an observed terminal result is local; it remains readable after takeover.
		if process.Generation != owner.Generation && !(consumeOutput && process.Status == "exited_unread" && status == "exited") {
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
		return s.recordEvidence(ctx, tx, owner, conversation, session, processID, "process", status, payload)
	})
}

// AbandonProcess stops scheduling a remote handle whose transport outcome is unknown while
// retaining every observed byte and exit code for inspection. It never records a remote exit.
func (s *Store) AbandonProcess(ctx context.Context, owner Owner, processID string, afterSequence *uint64, output *headTailBuffer, remoteOutcome, executorSessionID string) error {
	if remoteOutcome != "unknown" && remoteOutcome != "not_dispatched" {
		return ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		process, err := scanProcess(tx.QueryRowContext(ctx, `SELECT `+processColumns+`
			FROM sampler_process WHERE id=$1 AND conversation_id=$2 FOR UPDATE`, processID, owner.ConversationID))
		if errors.Is(err, sql.ErrNoRows) {
			return ErrMissing
		}
		if err != nil {
			return err
		}
		if process.Generation != owner.Generation {
			return &LostOwnershipError{Reason: "process belongs to another owner generation"}
		}
		if process.Status == "abandoned" {
			return nil
		}
		if process.Status == "exited" {
			return ErrPrecondition
		}
		if afterSequence == nil {
			afterSequence = process.AfterSequence
		}
		buffered, omitted := process.BufferedOutput, process.OmittedBytes
		if output != nil {
			buffered, omitted = output.retained(), output.omitted
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
		return s.recordEvidence(ctx, tx, owner, conversation, session, processID, "process", "abandoned", map[string]any{
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
			return ErrMissing
		}
		if err != nil {
			return err
		}
		if process.Generation != owner.Generation {
			return &LostOwnershipError{Reason: "process belongs to another owner generation"}
		}
		// Unread output keeps its head and tail, as upstream's pending buffer does.
		buffer, err := restoreHeadTailBuffer(unifiedExecOutputMaxBytes, process.BufferedOutput, process.OmittedBytes)
		if err != nil {
			return err
		}
		buffer.push(output)
		buffered := buffer.retained()
		status := process.Status
		var code any
		if exitCode != nil {
			code = *exitCode
		}
		if exited {
			status = "exited_unread"
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_process SET after_seq=$3,status=$4,exit_code=$5,buffered_output=$6,omitted_bytes=$7 WHERE id=$1 AND conversation_id=$2`,
			processID, owner.ConversationID, afterSequence, status, code, buffered, buffer.omitted); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		payload := map[string]any{"after_seq": afterSequence, "exit_code": exitCode, "unread_bytes": buffer.totalBytes()}
		return s.recordEvidence(ctx, tx, owner, conversation, session, processID, "process", status, payload)
	})
}

func (s *Store) CellResult(ctx context.Context, owner Owner, cellID, status string) error {
	if cellID == "" || status == "" {
		return ErrInvalid
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
		return s.recordEvidence(ctx, tx, owner, conversation, session, cellID, "cell", status, map[string]any{})
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

func (s *Store) CheckLiveCells(ctx context.Context, owner Owner, runtimeAvailable bool) error {
	state, err := s.ExecutionState(ctx, owner)
	if err != nil {
		return err
	}
	for _, cell := range state.Cells {
		if !runtimeAvailable || cell.Generation != owner.Generation {
			return &UnknownEffectError{Reason: "live JavaScript cell lost its host; automatic replay and V8 recovery are unsafe"}
		}
	}
	return nil
}

func (s *Store) RequestInterrupt(ctx context.Context, conversationID string, generation int) (string, error) {
	id := uuid.NewString()
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := scanConversation(tx.QueryRowContext(ctx, `SELECT `+conversationColumns+` FROM sampler_conversation WHERE id=$1 FOR UPDATE`, conversationID))
		if errors.Is(err, sql.ErrNoRows) {
			return ErrMissing
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
		return ErrInvalid
	}
	cleanupComplete, ok := result["cleanup_complete"].(bool)
	if !ok {
		return ErrInvalid
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
				return ErrMissing
			}
		}
		activeCall := ""
		if conversation.Pending != nil {
			pendingID := conversation.Pending.ID
			intent, err := intentFor(ctx, tx, pendingID)
			if err != nil {
				return err
			}
			call, _ := intent.Request["call"].(map[string]any)
			activeCall = stringValue(call["call_id"])
			interrupted := map[string]any{"status": "interrupted", "partial_effects_possible": true}
			for key, value := range result {
				interrupted[key] = value
			}
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
			request := map[string]any{"kind": "interrupt", "requests": requestIDs}
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
			outputs := interruptedOutputs(conversation.Items, activeCall)
			conversation.Items = append(conversation.Items, outputs...)
			if conversation.ContextItems != nil {
				conversation.ContextItems = append(conversation.ContextItems, outputs...)
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
		return s.recordEvidence(ctx, tx, owner, conversation, session, requestIDs[0], "interrupt", "result", result)
	})
}

func (s *Store) AppendInput(ctx context.Context, owner Owner, prompt string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending != nil {
			return &UnknownEffectError{Reason: "unresolved intent blocks new input; automatic replay is unsafe"}
		}
		item, err := newResponsesMessage("user", prompt)
		if err != nil {
			return err
		}
		conversation.Items = append(conversation.Items, item)
		if conversation.ContextItems != nil {
			conversation.ContextItems = append(conversation.ContextItems, item)
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

func (s *Store) checkInterrupt(ctx context.Context, tx *sql.Tx, owner Owner) error {
	return checkInterrupt(ctx, tx, owner)
}

func (s *Store) checkRecovery(ctx context.Context, tx *sql.Tx, owner Owner) error {
	var id string
	err := tx.QueryRowContext(ctx, `SELECT id::text FROM sampler_intent WHERE conversation_id=$1 AND generation<>$2 AND result IS NULL LIMIT 1`, owner.ConversationID, owner.Generation).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	return &UnknownEffectError{Reason: "earlier owner left an unresolved effect"}
}

func (s *Store) CheckRecovery(ctx context.Context, owner Owner) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		return s.checkRecovery(ctx, tx, owner)
	})
}

func pendingMatches(pending *PendingIntent, id string, kind PendingIntentKind) bool {
	if pending == nil {
		return false
	}
	if id != "" && pending.ID != id {
		return false
	}
	return kind == "" || pending.Kind == kind
}

func sameItems(left, right []ResponsesItem) bool {
	return equalResponsesItems(left, right)
}

func intentFor(ctx context.Context, tx *sql.Tx, id string) (*Intent, error) {
	var intent Intent
	var request, result []byte
	err := tx.QueryRowContext(ctx, `SELECT id::text,conversation_id::text,generation,request,result FROM sampler_intent WHERE id=$1`, id).Scan(
		&intent.ID, &intent.ConversationID, &intent.Generation, &request, &result,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrMissing
	}
	if err != nil {
		return nil, err
	}
	if intent.Request, err = decodeMap(request); err != nil {
		return nil, err
	}
	if intent.Result, err = decodeMap(result); err != nil {
		return nil, err
	}
	return &intent, nil
}

func hasUnresolvedIntent(ctx context.Context, tx *sql.Tx, conversationID, exceptID string) (bool, error) {
	var found bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL AND ($2='' OR id<>$2::uuid))`, conversationID, exceptID).Scan(&found)
	return found, err
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

func commandFor(ctx context.Context, tx *sql.Tx, sessionID, commandID string) (*Command, error) {
	var command Command
	var outcome int32
	var failure sql.NullString
	err := tx.QueryRowContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command WHERE session_id=$1 AND id=$2`, sessionID, commandID).Scan(
		&command.SessionID, &command.ID, &command.Payload, &command.Admission, &outcome, &failure,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrMissing
	}
	if err != nil {
		return nil, err
	}
	command.Outcome = samplerpb.CommandOutcome(outcome)
	if failure.Valid {
		command.FailureReason = &failure.String
	}
	return &command, nil
}

func setCommandOutcome(ctx context.Context, tx *sql.Tx, sessionID, commandID string, outcome samplerpb.CommandOutcome, reason *string) error {
	_, err := tx.ExecContext(ctx, `UPDATE sampler_command SET outcome=$3,failure_reason=$4 WHERE session_id=$1 AND id=$2`, sessionID, commandID, int32(outcome), nullableString(reason))
	return err
}

func unansweredCalls(items []ResponsesItem) []ResponsesToolCall {
	completed := map[string]struct{}{}
	for _, item := range items {
		if item.IsToolOutput() {
			completed[item.CallID] = struct{}{}
		}
	}
	var calls []ResponsesToolCall
	for _, item := range items {
		call, ok := item.ToolCall()
		if !ok {
			continue
		}
		if _, ok := completed[call.CallID]; !ok {
			calls = append(calls, call)
		}
	}
	return calls
}

func notDispatchedOutputs(items []ResponsesItem) []ResponsesItem {
	var outputs []ResponsesItem
	for _, call := range unansweredCalls(items) {
		outputs = append(outputs, call.OutputItem([]ResponsesContentPart{{Type: "input_text", Text: "Not dispatched because the turn failed."}}))
	}
	return outputs
}

func interruptedOutputs(items []ResponsesItem, activeCall string) []ResponsesItem {
	var outputs []ResponsesItem
	for _, call := range unansweredCalls(items) {
		text := "Not started because user interrupted the turn."
		if call.CallID == activeCall {
			text = "Interrupted by user. Partial effects may have occurred."
		}
		outputs = append(outputs, call.OutputItem([]ResponsesContentPart{{Type: "input_text", Text: text}}))
	}
	return outputs
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

package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/environmentcontext"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	OutboxChannel = "central_sampler_outbox"
	WorkChannel   = "central_sampler_work"
	TurnChannel   = "central_sampler_turn"
)

type rowScanner interface {
	Scan(dest ...any) error
}

// Store owns the central sampler's durable conversation, Session and evidence records.
type Store struct {
	db                         *sql.DB
	operationTimeout           time.Duration
	providerProgressCoalescing time.Duration
	providerRetryMaxBackoff    time.Duration
	eventBlockV2Writes         bool
	eventBlockProfile          eventblock.Profile
	contextSourceWrites        bool
	// now dates the model-visible environment context.
	now func() time.Time
}

// New uses the default provider timing when settings is nil.
func New(db *sql.DB, operationTimeout time.Duration, settings *samplersettings.Settings) *Store {
	if settings == nil {
		settings = &samplersettings.Settings{}
	}
	return &Store{
		db: db, operationTimeout: operationTimeout, now: time.Now,
		providerProgressCoalescing: settings.ProviderProgressLeaseCoalescing(),
		providerRetryMaxBackoff:    settings.ProviderRetryMaxBackoff(),
		eventBlockV2Writes:         settings.EventBlockV2Writes,
		eventBlockProfile:          settings.PackingProfile(),
		contextSourceWrites:        settings.ContextSourceWrites,
	}
}

func (s *Store) DB() *sql.DB { return s.db }

func (s *Store) transaction(ctx context.Context, operation string, f func(context.Context, *sql.Tx) error) error {
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	return postgres.NamedTransaction(ctx, s.db, sql.TxOptions{}, operation, func(tx *sql.Tx) error {
		// Migration 25's trigger invalidates a sampling recovery projection when an older
		// binary appends a journal row. This transaction is running the projection-aware
		// writer, which records dispatch and source-linked item metadata atomically.
		if err := markIndexedRecoveryWriter(ctx, tx); err != nil {
			return err
		}
		return f(ctx, tx)
	})
}

func markIndexedRecoveryWriter(ctx context.Context, tx *sql.Tx) error {
	_, err := tx.ExecContext(ctx, `SELECT set_config('agentplane.sampler_indexed_recovery_writer', 'on', TRUE)`)
	return err
}

func notify(ctx context.Context, tx *sql.Tx) error {
	return postgres.Notify(ctx, tx, OutboxChannel, "")
}

func notifyWork(ctx context.Context, tx *sql.Tx) error {
	return postgres.Notify(ctx, tx, WorkChannel, "")
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
	store := newContentDB(ctx, tx)
	conversation.executionSnapshots = map[protocol.Context]*modelExecutionSnapshot{}
	conversation.storedContexts = map[protocol.Context]contextCheckpoint{}
	var err error
	conversation.Transcript, err = conversation.loadNativeContext(store, conversation.API, conversation.transcriptContextID, conversation.transcriptControl)
	if err != nil {
		return err
	}
	if len(conversation.activeControl) > 0 {
		conversation.ActiveContext, err = conversation.loadNativeContext(store, conversation.API, conversation.activeContextID, conversation.activeControl)
	}
	return err
}

func nullableString(value *string) any {
	if value == nil {
		return nil
	}
	return *value
}

// ownership is a conversation's sampler lease. Its generation also keys the owner's interrupts and processes.
var ownership = lease.Table{
	Name: "sampler_conversation", Key: []string{"id"}, Holder: "owner", Until: "lease_until", Generation: "generation",
}

func (o Owner) lease() (lease.Lease, error) {
	token, err := uuid.Parse(o.Token)
	if err != nil {
		return lease.Lease{}, fmt.Errorf("owner token must be a UUID: %w", err)
	}
	return lease.Lease{Key: []any{o.ConversationID}, Holder: token, Generation: int64(o.Generation)}, nil
}

// fenceOwner fails with *LostOwnershipError unless owner holds the conversation's live lease, and otherwise holds
// the conversation locked to the end of tx.
func fenceOwner(ctx context.Context, tx *sql.Tx, owner Owner) error {
	held, err := owner.lease()
	if err != nil {
		return err
	}
	err = ownership.Fence(ctx, tx, held)
	if errors.Is(err, lease.ErrLost) {
		return &LostOwnershipError{}
	}
	return err
}

func scanConversation(row rowScanner) (*Conversation, error) {
	var (
		conversation      Conversation
		owner             sql.NullString
		pending           []byte
		executor          sql.NullString
		transcriptID      sql.NullString
		activeID          sql.NullString
		transcriptControl []byte
		activeControl     []byte
		instructions      sql.NullString
		contextTokens     sql.NullInt64
		shell             sql.NullString
		environment       []byte
	)
	if err := row.Scan(&conversation.ID, &conversation.Generation, &owner, &pending,
		&conversation.Stopped, &executor, &transcriptID, &activeID, &transcriptControl, &activeControl, &instructions, &contextTokens, &shell, &environment, &conversation.API); err != nil {
		return nil, err
	}
	if shell.Valid {
		conversation.ExecutorShell = &shell.String
	}
	if len(environment) != 0 {
		conversation.EnvironmentContext = &environmentcontext.Context{}
		if err := json.Unmarshal(environment, conversation.EnvironmentContext); err != nil {
			return nil, err
		}
	}
	if owner.Valid {
		conversation.Owner = &owner.String
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
	conversation.transcriptContextID = transcriptID.String
	conversation.activeContextID = activeID.String
	conversation.transcriptControl = transcriptControl
	conversation.activeControl = activeControl
	return &conversation, nil
}

const conversationColumns = `id::text, generation, owner::text, pending, stopped,
	executor_session, transcript_context_id, active_context_id, transcript_control, active_control, instructions, context_tokens, executor_shell, environment_context, api`

func lockOwner(ctx context.Context, tx *sql.Tx, owner Owner) (*Conversation, error) {
	row, err := lockOwnerWithContextRefs(ctx, tx, owner)
	if err != nil {
		return nil, err
	}
	if err := loadConversationItems(ctx, tx, row); err != nil {
		return nil, err
	}
	return row, nil
}

func lockOwnerWithContextRefs(ctx context.Context, tx *sql.Tx, owner Owner) (*Conversation, error) {
	row, err := scanConversation(tx.QueryRowContext(ctx,
		`SELECT `+conversationColumns+` FROM sampler_conversation WHERE id=$1 FOR UPDATE`, owner.ConversationID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, &LostOwnershipError{Reason: "conversation no longer exists"}
	}
	if err != nil {
		return nil, err
	}
	if err := fenceOwner(ctx, tx, owner); err != nil {
		return nil, err
	}
	return row, nil
}

// lockOwnerHeader fences an owner while reading the mutable fields needed by header-only paths.
// Provider and protocol stream events must not decode the retained transcript.
func lockOwnerHeader(ctx context.Context, tx *sql.Tx, owner Owner) (*Conversation, error) {
	var (
		conversation Conversation
		currentOwner sql.NullString
		pending      []byte
	)
	err := tx.QueryRowContext(ctx, `SELECT id::text,generation,owner::text,pending
		FROM sampler_conversation WHERE id=$1 FOR UPDATE`, owner.ConversationID).Scan(
		&conversation.ID, &conversation.Generation, &currentOwner, &pending,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, &LostOwnershipError{Reason: "conversation no longer exists"}
	}
	if err != nil {
		return nil, err
	}
	if err := fenceOwner(ctx, tx, owner); err != nil {
		return nil, err
	}
	if currentOwner.Valid {
		conversation.Owner = &currentOwner.String
	}
	if conversation.Pending, err = decodePendingIntent(pending); err != nil {
		return nil, err
	}
	return &conversation, nil
}

func lockSession(ctx context.Context, tx *sql.Tx, sessionID string) (*Conversation, *Session, error) {
	return lockSessionState(ctx, tx, sessionID, true)
}

func lockSessionHeader(ctx context.Context, tx *sql.Tx, sessionID string) (*Conversation, *Session, error) {
	return lockSessionState(ctx, tx, sessionID, false)
}

func lockSessionState(ctx context.Context, tx *sql.Tx, sessionID string, loadItems bool) (*Conversation, *Session, error) {
	var conversationID string
	err := tx.QueryRowContext(ctx, `SELECT c.id::text FROM sampler_conversation c
		JOIN sampler_session s ON s.conversation_id=c.id WHERE s.id=$1 FOR UPDATE OF c`, sessionID).Scan(&conversationID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, protocol.ErrMissing
	}
	if err != nil {
		return nil, nil, err
	}
	conversation, err := scanConversation(tx.QueryRowContext(ctx, `SELECT `+conversationColumns+` FROM sampler_conversation WHERE id=$1`, conversationID))
	if err != nil {
		return nil, nil, err
	}
	if loadItems {
		if err := loadConversationItems(ctx, tx, conversation); err != nil {
			return nil, nil, err
		}
	}
	session, err := scanSession(tx.QueryRowContext(ctx, `SELECT id, conversation_id::text, COALESCE(service_spiffe_id, account_uid), configuration, model, effort,
		source_id, cursor, active_turn, active_command, workspace_prepared, setup_state, setup_started, blocked, blocked_reason
		FROM sampler_session WHERE id=$1 FOR UPDATE`, sessionID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, protocol.ErrMissing
	}
	return conversation, session, err
}

func lockSessionForConversation(ctx context.Context, tx *sql.Tx, sessionID, conversationID string) (*Session, error) {
	session, err := scanSession(tx.QueryRowContext(ctx, `SELECT id, conversation_id::text, COALESCE(service_spiffe_id, account_uid), configuration, model, effort,
		source_id, cursor, active_turn, active_command, workspace_prepared, setup_state, setup_started, blocked, blocked_reason
		FROM sampler_session WHERE id=$1 AND conversation_id=$2 FOR UPDATE`, sessionID, conversationID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, protocol.ErrMissing
	}
	return session, err
}

func scanSession(row rowScanner) (*Session, error) {
	var session Session
	var activeTurn, activeCommand, blockedReason sql.NullString
	var cursor int64
	var setupState int32
	err := row.Scan(&session.ID, &session.ConversationID, &session.OwnerKey, &session.Configuration,
		&session.Model, &session.Effort, &session.SourceID, &cursor, &activeTurn, &activeCommand,
		&session.WorkspacePrepared, &setupState, &session.SetupStarted, &session.Blocked, &blockedReason)
	if err != nil {
		return nil, err
	}
	session.Cursor = uint64(cursor)
	session.SetupState = sessionpb.SetupState(setupState)
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

func (s *Store) appendEvent(ctx context.Context, tx *sql.Tx, session *Session, event *eventpb.Event) (*eventlogpb.EventEntry, error) {
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
	wire, err = eventcodec.Encode(wire)
	if err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_event(session_id,sequence,entry) VALUES($1,$2,$3)`, session.ID, session.Cursor, wire); err != nil {
		return nil, err
	}
	if s.eventBlockV2Writes {
		if err := sealJournalTail(ctx, tx, session.ID, session.Cursor, s.eventBlockProfile); err != nil {
			return nil, err
		}
	}
	if _, err := tx.ExecContext(ctx, `UPDATE sampler_session SET cursor=$2 WHERE id=$1`, session.ID, session.Cursor); err != nil {
		return nil, err
	}
	if err := notify(ctx, tx); err != nil {
		return nil, err
	}
	return entry, nil
}

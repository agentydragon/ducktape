package centralsampler

import (
	"context"
	"database/sql"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

func (s *Store) Read(ctx context.Context, owner Owner) (*Conversation, error) {
	var result *Conversation
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var err error
		result, err = lockOwner(ctx, tx, owner)
		return err
	})
	return result, err
}

func (s *Store) Create(ctx context.Context, api protocol.APIStyle, prompt string, instructions *string) (string, error) {
	if prompt == "" {
		return "", protocol.ErrInvalid
	}
	id := uuid.NewString()
	transcript, err := newNativeContext(api)
	if err != nil {
		return "", err
	}
	if err := transcript.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: prompt}}); err != nil {
		return "", err
	}
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_conversation(id,generation,owner,lease_until,pending,stopped,
   executor_session,instructions,context_tokens,api) VALUES($1,0,NULL,NULL,NULL,FALSE,NULL,$2,NULL,$3)`, id, nullableString(instructions), api); err != nil {
			return err
		}
		return updateConversation(ctx, tx, &Conversation{ID: id, API: api, Transcript: transcript})
	})
	return id, err
}

func (s *Store) Status(ctx context.Context, conversationID string) (map[string]any, error) {
	conversation, err := scanConversation(s.db.QueryRowContext(ctx, `SELECT `+conversationColumns+` FROM sampler_conversation WHERE id=$1`, conversationID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, protocol.ErrMissing
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

func (s *Store) ProviderMetadata(ctx context.Context, owner Owner) (protocol.SessionAttribution, error) {
	var sessionID string
	var activeTurn sql.NullString
	if err := s.db.QueryRowContext(ctx, `SELECT id,active_turn FROM sampler_session WHERE conversation_id=$1`, owner.ConversationID).Scan(&sessionID, &activeTurn); err != nil {
		return protocol.SessionAttribution{}, err
	}
	turn := sessionID
	if activeTurn.Valid {
		turn = activeTurn.String
	}
	return protocol.SessionAttribution{SessionID: sessionID, ThreadID: sessionID, TurnID: turn}, nil
}

func (s *Store) pgError(err error) error {
	var pgErr *pq.Error
	if errors.As(err, &pgErr) && string(pgErr.Code) == "23505" {
		return ErrConflict
	}
	return err
}

func updateConversation(ctx context.Context, tx *sql.Tx, conversation *Conversation) error {
	store := newContentStore(ctx, tx)
	transcriptID, err := store.storeConversationContext(conversation, conversation.Transcript)
	if err != nil {
		return err
	}
	transcriptControl, err := conversation.contextControl(conversation.Transcript)
	if err != nil {
		return err
	}
	var activeControl any
	var activeID string
	if conversation.ActiveContext != nil {
		activeID, err = store.storeConversationContext(conversation, conversation.ActiveContext)
		if err != nil {
			return err
		}
		control, err := conversation.contextControl(conversation.ActiveContext)
		if err != nil {
			return err
		}
		activeControl = []byte(control)
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
		transcript_context_id=$5,active_context_id=$6,transcript_control=$7,active_control=$8,
		context_tokens=$9,executor_shell=$10,environment_context=$11 WHERE id=$1`, conversation.ID,
		nullableJSON(pending), conversation.Stopped, nullableString(conversation.ExecutorSession), nullableContextID(transcriptID), nullableContextID(activeID), []byte(transcriptControl), activeControl, contextTokens,
		nullableString(conversation.ExecutorShell), nullableJSON(environment))
	return err
}

func nullableJSON(value string) any {
	if value == "" || value == "null" {
		return nil
	}
	return value
}

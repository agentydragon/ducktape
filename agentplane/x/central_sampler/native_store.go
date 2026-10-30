package centralsampler

import (
	"context"
	"database/sql"
)

func (s *Store) PrepareSamplingContext(ctx context.Context, owner Owner, state *ExecutionState) (*Conversation, error) {
	var conversation *Conversation
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var err error
		conversation, err = lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending != nil {
			return ErrPrecondition
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := s.recordEnvironmentContext(conversation, session); err != nil {
			return err
		}
		if err := conversation.apply(func(native NativeContext) error { return native.AppendExecutionState(state) }); err != nil {
			return err
		}
		return updateConversation(ctx, tx, conversation)
	})
	return conversation, err
}

func (s *Store) GetSessionForConversation(ctx context.Context, owner Owner) (*Session, error) {
	var session *Session
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwnerHeader(ctx, tx, owner); err != nil {
			return err
		}
		var err error
		session, err = sessionForConversation(ctx, tx, owner.ConversationID)
		return err
	})
	return session, err
}

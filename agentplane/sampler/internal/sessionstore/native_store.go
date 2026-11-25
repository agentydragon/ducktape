package sessionstore

import (
	"context"
	"database/sql"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

func (s *Store) PrepareSamplingContext(ctx context.Context, owner Owner) (*Conversation, error) {
	var conversation *Conversation
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		var err error
		conversation, err = lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending != nil {
			return protocol.ErrPrecondition
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := s.applySamplingContext(ctx, tx, conversation, session); err != nil {
			return err
		}
		return s.updateConversation(ctx, tx, conversation)
	})
	return conversation, err
}

func (s *Store) applySamplingContext(ctx context.Context, tx *sql.Tx, conversation *Conversation, session *Session) error {
	objects := newContentDB(ctx, tx)
	if err := s.recordEnvironmentContext(objects, conversation, session, false); err != nil {
		return err
	}
	return objects.Save()
}

func (s *Store) GetSessionForConversation(ctx context.Context, owner Owner) (*Session, error) {
	var session *Session
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		if _, err := lockOwnerHeader(ctx, tx, owner); err != nil {
			return err
		}
		var err error
		session, err = sessionForConversation(ctx, tx, owner.ConversationID)
		return err
	})
	return session, err
}

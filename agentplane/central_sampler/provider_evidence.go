package centralsampler

import (
	"context"
	"database/sql"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

type ResponseEvidence struct {
	Store    *Store
	Owner    Owner
	IntentID string
}

func (e *ResponseEvidence) Record(ctx context.Context, payload protocol.ProviderEvidence, observations ...*eventpb.Event) error {
	return e.Store.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwnerHeader(ctx, tx, e.Owner)
		if err != nil {
			return err
		}
		if !pendingMatches(conversation.Pending, e.IntentID, PendingIntentSampling) &&
			!pendingMatches(conversation.Pending, e.IntentID, PendingIntentCompaction) {
			return protocol.ErrPrecondition
		}
		var current bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS(
			SELECT 1 FROM sampler_intent WHERE id=$1 AND conversation_id=$2 AND generation=$3
				AND result IS NULL AND request->>'kind'=$4
		)`, e.IntentID, conversation.ID, e.Owner.Generation, string(conversation.Pending.Kind)).Scan(&current); err != nil {
			return err
		}
		if !current {
			return protocol.ErrPrecondition
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		sources, err := e.Store.recordEvidenceWithSources(ctx, tx, e.Owner, conversation, session, e.IntentID, payload.Kind(), eventpb.ExecutionProgress_PHASE_RECEIVED, payload)
		if err != nil {
			return err
		}
		for _, observation := range observations {
			if observation == nil {
				continue
			}
			observation.SourceSequences = sources
			if _, err := appendEvent(ctx, tx, session, observation); err != nil {
				return err
			}
		}
		return nil
	})
}

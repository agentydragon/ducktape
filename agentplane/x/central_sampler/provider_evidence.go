package centralsampler

import (
	"context"
	"database/sql"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
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
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		sources, err := e.Store.recordEvidenceWithSources(ctx, tx, e.Owner, conversation, session, e.IntentID, string(payload.Kind()), "received", payload)
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

package sessionstore

import (
	"context"
	"database/sql"
	"errors"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

type ResponseEvidence struct {
	Store         *Store
	Owner         Owner
	IntentID      string
	ProgressLease time.Duration
}

func (e *ResponseEvidence) Record(ctx context.Context, payload protocol.ProviderEvidence, semanticProgress bool, observations ...*eventpb.Event) error {
	if semanticProgress && e.ProgressLease <= 0 {
		return protocol.ErrInvalid
	}
	return e.Store.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwnerHeader(ctx, tx, e.Owner)
		if err != nil {
			return err
		}
		if !pendingMatches(conversation.Pending, e.IntentID, PendingIntentSampling) &&
			!pendingMatches(conversation.Pending, e.IntentID, PendingIntentCompaction) {
			return protocol.ErrPrecondition
		}
		// The conversation lock serializes attempt settlement. The renewal itself checks the
		// attempt's DB-clock deadline so progress cannot resurrect an expired attempt.
		const currentAttempt = ` WHERE id=$1 AND conversation_id=$2 AND generation=$3
			AND result IS NULL AND request->>'kind'=$4 AND provider_attempt_lease_until > clock_timestamp()`
		query := `SELECT 1 FROM sampler_intent` + currentAttempt
		args := []any{e.IntentID, conversation.ID, e.Owner.Generation, string(conversation.Pending.Kind)}
		if semanticProgress {
			query = `UPDATE sampler_intent SET provider_attempt_lease_until=clock_timestamp()+make_interval(secs=>$5)` + currentAttempt + ` RETURNING 1`
			args = append(args, e.ProgressLease.Seconds())
		}
		var current int
		err = tx.QueryRowContext(ctx, query, args...).Scan(&current)
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrPrecondition
		}
		if err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		sources, err := e.Store.recordEvidenceWithSources(ctx, tx, conversation, session, e.IntentID, payload.Kind(), eventpb.ExecutionProgress_PHASE_RECEIVED, payload)
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

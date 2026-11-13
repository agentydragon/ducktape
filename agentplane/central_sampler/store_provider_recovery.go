package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
)

const providerRetrySuppressedHostLoss = "host_loss"

type ProviderFinishReadback struct {
	Open         bool
	Completed    bool
	ResultStatus string
}

// ReadProviderFinish reads the exact provider intent under the current owner fence after an
// ambiguous Finish result. Open permits persistence-only retry using the already received result;
// Completed confirms the transaction committed. Other states remain unresolved to the caller.
func (s *Store) ReadProviderFinish(ctx context.Context, owner Owner, intentID string) (ProviderFinishReadback, error) {
	var state ProviderFinishReadback
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwnerHeader(ctx, tx, owner)
		if err != nil {
			return err
		}
		var kind, resultStatus string
		var generation int
		var hasResultStatus bool
		var result []byte
		err = tx.QueryRowContext(ctx, `SELECT request->>'kind',generation,result,COALESCE(result->>'status',''),COALESCE(result ? 'status',FALSE)
			FROM sampler_intent WHERE id=$1 AND conversation_id=$2 AND request->>'kind' IN ('sampling','compaction')`,
			intentID, conversation.ID).Scan(&kind, &generation, &result, &resultStatus, &hasResultStatus)
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		if err != nil {
			return err
		}
		if generation != owner.Generation {
			return &LostOwnershipError{Reason: "provider intent belongs to another owner generation"}
		}
		state.ResultStatus = resultStatus
		pending := pendingMatches(conversation.Pending, intentID, PendingIntentKind(kind))
		state.Open = result == nil && pending
		// Finish stores the provider audit directly (which may contain nested provider status
		// fields), while every sampler-owned rejection/abandonment has a top-level status key.
		// Presence, rather than a non-empty value, keeps malformed or future terminal markers
		// from being mistaken for a successful commit.
		state.Completed = result != nil && !hasResultStatus && !pending
		return nil
	})
	return state, err
}

func (s *Store) beginProviderAfterHostLoss(ctx context.Context, owner Owner, sessionID, previousIntentID string,
	request IntentPayload, original protocol.Context, prepare requestPreparation, policy ProviderAttemptPolicy,
) (string, []byte, error) {
	kind := request.Kind
	if request.ProviderRequest == nil || prepare == nil || policy.validate() != nil ||
		(kind != PendingIntentSampling && kind != PendingIntentCompaction) ||
		(kind == PendingIntentCompaction && original == nil) || (kind == PendingIntentSampling && original != nil) {
		return "", nil, protocol.ErrInvalid
	}
	id := uuid.NewString()
	var wire []byte
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending != nil || conversation.Stopped {
			return protocol.ErrPrecondition
		}
		if err := checkInterrupt(ctx, tx, owner); err != nil {
			return err
		}
		if kind == PendingIntentCompaction && !sameNativeContext(conversation.Context(), original) {
			return protocol.ErrPrecondition
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if session.ConversationID != owner.ConversationID || session.ActiveCommand == nil || session.Blocked {
			return protocol.ErrPrecondition
		}
		command, err := commandFor(ctx, tx, session.ID, *session.ActiveCommand)
		if err != nil {
			return err
		}
		if command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
			return protocol.ErrPrecondition
		}
		var previous *ProviderAttempt
		previous, err = scanProviderAttempt(tx.QueryRowContext(ctx, `SELECT `+providerAttemptColumns+`
			FROM sampler_intent i WHERE i.id=$1 AND i.conversation_id=$2 AND i.request->>'kind'=$3
				AND i.result->>'status'='abandoned' AND i.result->>'provider_retry_suppressed'='host_loss'
				AND i.provider_command_id=$4 AND NOT EXISTS(SELECT 1 FROM sampler_intent n WHERE n.previous_provider_intent_id=i.id)
			FOR UPDATE`, previousIntentID, owner.ConversationID, string(kind), *session.ActiveCommand))
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrPrecondition
		}
		if err != nil {
			return err
		}
		if previous.Number >= previous.Budget || previous.Budget != policy.Budget ||
			previous.FailureClass != "" && previous.FailureClass != ProviderAttemptTransient {
			return protocol.ErrPrecondition
		}
		if kind == PendingIntentCompaction {
			liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
			if err != nil {
				return err
			}
			unresolved, err := hasUnresolvedIntent(ctx, tx, conversation.ID, "")
			if err != nil {
				return err
			}
			if liveCell || unresolved {
				return protocol.ErrPrecondition
			}
		}
		prepared, rendered, err := prepare(newContentStore(ctx, tx), conversation, *request.ProviderRequest)
		if err != nil {
			return err
		}
		wire = rendered
		request.ProviderRequest = &prepared
		request.ProviderRequest.SourceNativeContextID = conversation.nativeContextID()
		request.ProviderRequest.SourceControl, err = conversation.contextControl(conversation.Context())
		if err != nil {
			return err
		}
		requestWire, err := json.Marshal(request)
		if err != nil {
			return err
		}
		previousID := previous.IntentID
		if err := insertProviderAttempt(ctx, tx, id, owner, string(requestWire), session, policy, previous.StepID, &previousID, previous.Number+1, previous.Budget); err != nil {
			return err
		}
		if err := saveProviderRequest(ctx, tx, id, conversation.ID, request.ProviderRequest); err != nil {
			return err
		}
		conversation.Pending = &PendingIntent{ID: id, Kind: kind}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, id, IntentKind(kind).progressKind(), eventpb.ExecutionProgress_PHASE_DISPATCH, request.dispatchEvidence(id))
	})
	if err != nil {
		return "", nil, err
	}
	return id, wire, nil
}

func (s *Store) BeginSamplingAfterHostLoss(ctx context.Context, owner Owner, sessionID, previousIntentID string,
	request IntentPayload, prepare requestPreparation, policy ProviderAttemptPolicy,
) (string, []byte, error) {
	if request.Kind != PendingIntentSampling {
		return "", nil, protocol.ErrInvalid
	}
	return s.beginProviderAfterHostLoss(ctx, owner, sessionID, previousIntentID, request, nil, prepare, policy)
}

func (s *Store) BeginCompactionAfterHostLoss(ctx context.Context, owner Owner, sessionID, previousIntentID string,
	request IntentPayload, original protocol.Context, prepare requestPreparation, policy ProviderAttemptPolicy,
) (string, []byte, error) {
	if request.Kind != PendingIntentCompaction {
		return "", nil, protocol.ErrInvalid
	}
	return s.beginProviderAfterHostLoss(ctx, owner, sessionID, previousIntentID, request, original, prepare, policy)
}

package centralsampler

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
)

const (
	maxProviderAttemptBudget = 16
	maxProviderBackoff       = 30 * time.Second
)

func (policy ProviderAttemptPolicy) validate() error {
	if policy.Budget <= 0 || policy.Budget > maxProviderAttemptBudget || policy.Lease <= 0 {
		return protocol.ErrInvalid
	}
	return nil
}

func runtimeProviderAttemptPolicy(settings samplersettings.Settings) ProviderAttemptPolicy {
	return ProviderAttemptPolicy{Budget: 3, Lease: settings.ProviderAttemptLease()}
}

func validateProviderFailure(failure ProviderAttemptFailure) error {
	if failure.Reason == "" || len(failure.Reason) > 4096 {
		return protocol.ErrInvalid
	}
	switch failure.Class {
	case ProviderAttemptTransient:
		if failure.RetryAfter <= 0 || failure.RetryAfter > maxProviderBackoff {
			return protocol.ErrInvalid
		}
	case ProviderAttemptPermanent, ProviderAttemptInterrupted, ProviderAttemptDraining:
		if failure.RetryAfter != 0 {
			return protocol.ErrInvalid
		}
	default:
		return protocol.ErrInvalid
	}
	return nil
}

func insertProviderAttempt(ctx context.Context, tx *sql.Tx, id string, owner Owner, requestWire string, session *Session,
	policy ProviderAttemptPolicy, stepID string, previousID *string, number, budget int,
) error {
	if err := policy.validate(); err != nil || number <= 0 || budget < number || budget > maxProviderAttemptBudget {
		return protocol.ErrInvalid
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(
		id,conversation_id,generation,request,result,provider_step_id,provider_attempt_no,provider_attempt_budget,
		provider_attempt_lease_until,previous_provider_intent_id,provider_command_id)
		VALUES($1,$2,$3,$4,NULL,$5,$6,$7,clock_timestamp()+make_interval(secs=>$8),$9,$10)`,
		id, owner.ConversationID, owner.Generation, requestWire, stepID, number, budget, policy.Lease.Seconds(), nullableString(previousID), nullableString(session.ActiveCommand))
	return err
}

func requireLiveProviderAttempt(ctx context.Context, tx *sql.Tx, intentID string) error {
	var leaseUntil time.Time
	err := tx.QueryRowContext(ctx, `SELECT provider_attempt_lease_until FROM sampler_intent
		WHERE id=$1 AND result IS NULL AND request->>'kind' IN ('sampling','compaction') FOR UPDATE`, intentID).Scan(&leaseUntil)
	if errors.Is(err, sql.ErrNoRows) {
		return protocol.ErrPrecondition
	}
	if err != nil {
		return err
	}
	var live bool
	if err := tx.QueryRowContext(ctx, `SELECT $1::timestamptz > clock_timestamp()`, leaseUntil).Scan(&live); err != nil {
		return err
	}
	if !live {
		return protocol.ErrPrecondition
	}
	return nil
}

func settleIntentResult(ctx context.Context, tx *sql.Tx, intentID string, resultWire string) error {
	tag, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1 AND result IS NULL`, intentID, resultWire)
	if err != nil {
		return err
	}
	count, err := tag.RowsAffected()
	if err != nil {
		return err
	}
	if count != 1 {
		return protocol.ErrPrecondition
	}
	return nil
}

const providerAttemptColumns = `id::text,provider_step_id::text,COALESCE(previous_provider_intent_id::text,''),
	COALESCE(provider_command_id,''),request->>'kind',provider_attempt_no,provider_attempt_budget,generation,
	provider_attempt_lease_until,provider_attempt_lease_until<=clock_timestamp(),provider_retry_not_before,
	COALESCE(result->>'failure_class',''),COALESCE(result->>'failure_reason',''),COALESCE(result->>'status',''),
	COALESCE(result->>'provider_retry_suppressed',''),
	(COALESCE(result->>'failure_class','')='transient' AND provider_retry_not_before IS NOT NULL AND provider_attempt_no<provider_attempt_budget),
	COALESCE(COALESCE(result->>'failure_class','')='transient' AND provider_retry_not_before<=clock_timestamp() AND provider_attempt_no<provider_attempt_budget,FALSE),
	GREATEST(COALESCE(EXTRACT(EPOCH FROM (provider_retry_not_before-clock_timestamp())),0),0),request`

func scanProviderAttempt(row rowScanner) (*ProviderAttempt, error) {
	var attempt ProviderAttempt
	var kind, failureClass, retrySuppressed string
	var number, budget int
	var retryAt sql.NullTime
	var retryInSeconds float64
	var requestWire []byte
	if err := row.Scan(&attempt.IntentID, &attempt.StepID, &attempt.PreviousID, &attempt.CommandID, &kind, &number, &budget,
		&attempt.Generation, &attempt.LeaseUntil, &attempt.Expired, &retryAt, &failureClass, &attempt.FailureReason, &attempt.ResultStatus, &retrySuppressed,
		&attempt.Retryable, &attempt.Ready, &retryInSeconds, &requestWire); err != nil {
		return nil, err
	}
	attempt.Kind, attempt.FailureClass, attempt.Number, attempt.Budget = PendingIntentKind(kind), ProviderAttemptFailureClass(failureClass), number, budget
	attempt.RetrySuppressed = ProviderRetrySuppression(retrySuppressed)
	attempt.RetryIn = time.Duration(retryInSeconds * float64(time.Second))
	var request IntentPayload
	if err := json.Unmarshal(requestWire, &request); err != nil {
		return nil, err
	}
	attempt.ProviderRequest = request.ProviderRequest
	if retryAt.Valid {
		attempt.RetryAt = &retryAt.Time
	}
	return &attempt, nil
}

func sameJSONValue(left, right []byte) bool {
	leftValue, err := protocol.DecodeJSONValue(left)
	if err != nil {
		return false
	}
	rightValue, err := protocol.DecodeJSONValue(right)
	if err != nil {
		return false
	}
	leftCanonical, err := json.Marshal(leftValue)
	if err != nil {
		return false
	}
	rightCanonical, err := json.Marshal(rightValue)
	return err == nil && bytes.Equal(leftCanonical, rightCanonical)
}

// CurrentProviderAttempt exposes the exact pending sampling/compaction intent, including its
// originating generation and a PostgreSQL-clock expiry observation. A current Session owner may
// abandon an unresolved older-generation attempt, but only the original generation can renew or
// finish it.
func (s *Store) CurrentProviderAttempt(ctx context.Context, owner Owner, sessionID string) (*ProviderAttempt, error) {
	var attempt *ProviderAttempt
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending == nil || (conversation.Pending.Kind != PendingIntentSampling && conversation.Pending.Kind != PendingIntentCompaction) {
			return nil
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if session.ConversationID != owner.ConversationID {
			return protocol.ErrPrecondition
		}
		attempt, err = scanProviderAttempt(tx.QueryRowContext(ctx, `SELECT `+providerAttemptColumns+`
			FROM sampler_intent WHERE id=$1 AND conversation_id=$2 AND result IS NULL AND request->>'kind'=$3`,
			conversation.Pending.ID, owner.ConversationID, string(conversation.Pending.Kind)))
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrPrecondition
		}
		return err
	})
	return attempt, err
}

// ProviderRecoveryFrontier returns the terminal explicit provider-abandon frontier belonging to
// the still-accepted original command, including whether another attempt is allowed and its
// DB-clock readiness. RetrySuppressed describes an interrupted or host-lost frontier; the latter
// requires a new model-visible context and cannot reuse its frozen request. An expired attempt
// never transfers Session ownership or makes a retry due.
func (s *Store) ProviderRecoveryFrontier(ctx context.Context, owner Owner, sessionID string) (*ProviderAttempt, error) {
	var attempt *ProviderAttempt
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if session.ConversationID != owner.ConversationID || session.ActiveCommand == nil || conversation.Pending != nil || conversation.Stopped || session.Blocked {
			return nil
		}
		command, err := commandFor(ctx, tx, session.ID, *session.ActiveCommand)
		if errors.Is(err, protocol.ErrMissing) {
			return nil
		}
		if err != nil {
			return err
		}
		if command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
			return nil
		}
		var interrupted bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_interrupt WHERE conversation_id=$1 AND generation=$2 AND result IS NULL)`, owner.ConversationID, owner.Generation).Scan(&interrupted); err != nil {
			return err
		}
		if interrupted {
			return nil
		}
		attempt, err = scanProviderAttempt(tx.QueryRowContext(ctx, `SELECT `+providerAttemptColumns+`
			FROM sampler_intent i WHERE i.conversation_id=$1 AND i.provider_command_id=$2
				AND i.result->>'status'='abandoned'
				AND (i.result->>'failure_class' IS NOT NULL OR i.result->>'provider_retry_suppressed'='host_loss')
				AND NOT EXISTS(SELECT 1 FROM sampler_intent n WHERE n.previous_provider_intent_id=i.id)
			ORDER BY i.id LIMIT 1`, owner.ConversationID, *session.ActiveCommand))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		return err
	})
	return attempt, err
}

// RenewProviderAttempt extends only a still-live unresolved attempt under its original owner
// generation. The conversation fence is acquired before touching the attempt row.
func (s *Store) RenewProviderAttempt(ctx context.Context, owner Owner, intentID string, lease time.Duration) error {
	if lease <= 0 {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending == nil || conversation.Pending.ID != intentID ||
			(conversation.Pending.Kind != PendingIntentSampling && conversation.Pending.Kind != PendingIntentCompaction) {
			return protocol.ErrPrecondition
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if intent.ConversationID != owner.ConversationID || intent.Generation != owner.Generation {
			return &LostOwnershipError{Reason: "intent belongs to another owner generation"}
		}
		tag, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET provider_attempt_lease_until=clock_timestamp()+make_interval(secs=>$2)
			WHERE id=$1 AND result IS NULL AND provider_attempt_lease_until>clock_timestamp() AND request->>'kind' IN ('sampling','compaction')`, intentID, lease.Seconds())
		if err != nil {
			return err
		}
		count, err := tag.RowsAffected()
		if err != nil {
			return err
		}
		if count != 1 {
			return protocol.ErrPrecondition
		}
		return nil
	})
}

// AbandonProviderAttempt settles only the exact pending provider request. Its raw provider
// observations remain untouched; sampling partial items are reconciled as ABSENT. Retry time is
// written on this predecessor, so a crash before creating its successor loses no retry work.
func (s *Store) AbandonProviderAttempt(ctx context.Context, owner Owner, intentID string, failure ProviderAttemptFailure) error {
	if err := validateProviderFailure(failure); err != nil {
		return err
	}
	return s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending == nil || conversation.Pending.ID != intentID ||
			(conversation.Pending.Kind != PendingIntentSampling && conversation.Pending.Kind != PendingIntentCompaction) {
			return protocol.ErrPrecondition
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if intent.ConversationID != owner.ConversationID || IntentKind(conversation.Pending.Kind) != intent.Kind {
			return protocol.ErrPrecondition
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		var attemptNo, budget int
		var commandID sql.NullString
		var requestWire []byte
		if err := tx.QueryRowContext(ctx, `SELECT provider_attempt_no,provider_attempt_budget,provider_command_id,request
			FROM sampler_intent WHERE id=$1 AND result IS NULL FOR UPDATE`, intentID).Scan(&attemptNo, &budget, &commandID, &requestWire); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return protocol.ErrPrecondition
			}
			return err
		}
		var request IntentPayload
		if err := json.Unmarshal(requestWire, &request); err != nil {
			return err
		}
		frozenRequest := request.ProviderRequest != nil && request.SourceNativeContextID != "" && len(request.SourceControl) != 0
		abandoned := map[string]any{
			"status": IntentResultAbandoned, "provider_completion_unknown": true,
			"output_committed": false, "automatic_replay": false,
			"failure_class": failure.Class, "failure_reason": failure.Reason,
		}
		wire, err := marshal(abandoned)
		if err != nil {
			return err
		}
		if err := settleIntentResult(ctx, tx, intentID, wire); err != nil {
			return err
		}
		if failure.Class == ProviderAttemptTransient && attemptNo < budget && frozenRequest && commandID.Valid && session.ActiveCommand != nil && commandID.String == *session.ActiveCommand {
			_, err = tx.ExecContext(ctx, `UPDATE sampler_intent SET provider_retry_not_before=clock_timestamp()+make_interval(secs=>$2)
				WHERE id=$1`, intentID, failure.RetryAfter.Seconds())
			if err != nil {
				return err
			}
		}
		abandonedSources, err := s.recordEvidenceWithSources(ctx, tx, owner, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_ABANDONED, abandoned)
		if err != nil {
			return err
		}
		if intent.Kind == IntentSampling {
			reconciliation, err := samplingReconciliation(ctx, tx, session, intentID)
			if err != nil {
				return err
			}
			reconciliation.SourceSequences = append(reconciliation.SourceSequences, abandonedSources...)
			if _, err := appendEvent(ctx, tx, session, reconciliation); err != nil {
				return err
			}
		}
		conversation.Pending = nil
		return updateConversation(ctx, tx, conversation)
	})
}

func (s *Store) beginProviderRetry(ctx context.Context, owner Owner, previousIntentID string, original protocol.Context, kind PendingIntentKind,
	policy ProviderAttemptPolicy,
) (string, []byte, error) {
	if policy.validate() != nil || (kind != PendingIntentSampling && kind != PendingIntentCompaction) {
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
		session, err := sessionForConversation(ctx, tx, owner.ConversationID)
		if err != nil {
			return err
		}
		if session.ActiveCommand == nil || session.Blocked {
			return protocol.ErrPrecondition
		}
		command, err := commandFor(ctx, tx, session.ID, *session.ActiveCommand)
		if err != nil {
			return err
		}
		if command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
			return protocol.ErrPrecondition
		}
		var predecessor *ProviderAttempt
		predecessor, err = scanProviderAttempt(tx.QueryRowContext(ctx, `SELECT `+providerAttemptColumns+`
			FROM sampler_intent i WHERE i.id=$1 AND i.conversation_id=$2 AND i.result->>'status'='abandoned'
				AND i.provider_retry_not_before<=clock_timestamp() AND i.provider_attempt_no<i.provider_attempt_budget
				AND i.provider_command_id=$3
				AND NOT EXISTS(SELECT 1 FROM sampler_intent n WHERE n.previous_provider_intent_id=i.id)
			FOR UPDATE`, previousIntentID, owner.ConversationID, *session.ActiveCommand))
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrPrecondition
		}
		if err != nil {
			return err
		}
		if predecessor.Kind != kind || predecessor.Budget != policy.Budget {
			return protocol.ErrPrecondition
		}
		var requestWire []byte
		if err := tx.QueryRowContext(ctx, `SELECT request FROM sampler_intent WHERE id=$1`, previousIntentID).Scan(&requestWire); err != nil {
			return err
		}
		var request IntentPayload
		if err := json.Unmarshal(requestWire, &request); err != nil {
			return err
		}
		if request.Kind != kind || request.ProviderRequest == nil || request.API != conversation.API {
			return protocol.ErrPrecondition
		}
		currentControl, err := conversation.contextControl(conversation.Context())
		if err != nil {
			return err
		}
		if request.SourceNativeContextID == "" || request.SourceNativeContextID != conversation.nativeContextID() ||
			len(request.SourceControl) == 0 || !sameJSONValue(request.SourceControl, currentControl) {
			return fmt.Errorf("provider retry source context changed: %w", protocol.ErrPrecondition)
		}
		wire, err = renderProviderRequest(newContentStore(ctx, tx), *request.ProviderRequest)
		if err != nil {
			return err
		}
		retryRequestWire, err := marshal(request)
		if err != nil {
			return err
		}
		attemptNo := predecessor.Number + 1
		if err := insertProviderAttempt(ctx, tx, id, owner, retryRequestWire, session, policy, predecessor.StepID, &previousIntentID, attemptNo, predecessor.Budget); err != nil {
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

// BeginSamplingRetry creates a fresh intent for a due sampling frontier and returns the exact
// provider body rendered from the original immutable request recipe.
func (s *Store) BeginSamplingRetry(ctx context.Context, owner Owner, previousIntentID string, policy ProviderAttemptPolicy) (string, []byte, error) {
	return s.beginProviderRetry(ctx, owner, previousIntentID, nil, PendingIntentSampling, policy)
}

// BeginCompactionRetry preserves the original compaction context and returns its exact frozen
// provider body. It refuses if either the caller's native context or the persisted source changed.
func (s *Store) BeginCompactionRetry(ctx context.Context, owner Owner, previousIntentID string, original protocol.Context, policy ProviderAttemptPolicy) (string, []byte, error) {
	if original == nil {
		return "", nil, protocol.ErrInvalid
	}
	return s.beginProviderRetry(ctx, owner, previousIntentID, original, PendingIntentCompaction, policy)
}

package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/google/uuid"
)

// ProviderAttemptSnapshot retains the committed native contexts for one fresh sampling attempt.
// It is only reusable with the matching owner, pending intent, native heads and native controls.
type ProviderAttemptSnapshot struct {
	owner        Owner
	intentID     string
	conversation *Conversation
}

// BeginPreparedSampling commits the sampling context and the provider intent together.
// source is the Conversation previously read by the caller and remains unchanged.
func (s *Store) BeginPreparedSampling(ctx context.Context, owner Owner, source *Conversation,
	request IntentPayload, preparation *providerrequest.Preparation, policy ProviderAttemptPolicy,
) (string, []byte, *ProviderAttemptSnapshot, error) {
	if source == nil || request.Kind != PendingIntentSampling || request.ProviderRequest == nil || preparation == nil || policy.validate() != nil {
		return "", nil, nil, protocol.ErrInvalid
	}
	id := uuid.NewString()
	var wire []byte
	var snapshot *ProviderAttemptSnapshot
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwnerWithContextRefs(ctx, tx, owner)
		if err != nil {
			return err
		}
		if err := checkIntentStart(ctx, tx, owner, conversation); err != nil {
			return err
		}
		if !conversation.reuseNativeContexts(source) {
			if err := loadConversationItems(ctx, tx, conversation); err != nil {
				return err
			}
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := s.applySamplingContext(ctx, tx, conversation, session); err != nil {
			return err
		}
		// Request projection needs durable context references for the just-appended records.
		if err := s.checkpointConversationContexts(ctx, tx, conversation); err != nil {
			return err
		}
		wire, err = s.beginIntent(ctx, tx, owner, conversation, session, id, request, requestPreparationFor(preparation), &policy)
		if err != nil {
			return err
		}
		snapshot = &ProviderAttemptSnapshot{owner: owner, intentID: id, conversation: conversation}
		return nil
	})
	if err != nil {
		return "", nil, nil, err
	}
	return id, wire, snapshot, nil
}

// InvalidSamplingContinuationError indicates that the returned records failed native append or validation.
type InvalidSamplingContinuationError struct{ Cause error }

func (e *InvalidSamplingContinuationError) Error() string { return e.Cause.Error() }
func (e *InvalidSamplingContinuationError) Unwrap() error { return e.Cause }

// FinishPreparedSampling applies and validates the provider's result on the current native contexts.
// It reloads them when the committed context references differ from those in snapshot.
func (s *Store) FinishPreparedSampling(ctx context.Context, owner Owner, intentID string, snapshot *ProviderAttemptSnapshot,
	result any, records []json.RawMessage, contextTokens *int, validate func(protocol.Context) error,
) error {
	if snapshot == nil || snapshot.conversation == nil || snapshot.owner != owner || snapshot.intentID != intentID {
		return protocol.ErrPrecondition
	}
	return s.finish(ctx, owner, intentID, result, contextTokens, snapshot, func(conversation *Conversation) error {
		if err := conversation.appendProviderRecords(records, intentID); err != nil {
			return &InvalidSamplingContinuationError{Cause: err}
		}
		if validate != nil {
			if err := validate(conversation.Transcript); err != nil {
				return &InvalidSamplingContinuationError{Cause: err}
			}
		}
		return nil
	})
}

func (conversation *Conversation) reuseNativeContexts(source *Conversation) bool {
	if conversation.ID != source.ID || conversation.Generation != source.Generation || conversation.API != source.API ||
		conversation.Owner == nil || source.Owner == nil || *conversation.Owner != *source.Owner ||
		conversation.transcriptContextID != source.transcriptContextID || conversation.activeContextID != source.activeContextID ||
		!sameContextControl(conversation.transcriptControl, source.transcriptControl) ||
		!sameContextControl(conversation.activeControl, source.activeControl) ||
		source.Transcript == nil || !source.hasContextCheckpoint(source.Transcript, source.transcriptContextID, source.transcriptControl) ||
		!source.hasContextCheckpoint(source.ActiveContext, source.activeContextID, source.activeControl) {
		return false
	}

	// Each transaction owns its working copies so rollback leaves the cached contexts reusable.
	conversation.storedContexts = make(map[protocol.Context]contextCheckpoint, len(source.storedContexts))
	conversation.executionSnapshots = make(map[protocol.Context]*modelExecutionSnapshot, len(source.executionSnapshots))
	conversation.Transcript = conversation.cloneCheckpointedContext(source, source.Transcript)
	conversation.ActiveContext = conversation.cloneCheckpointedContext(source, source.ActiveContext)
	return true
}

func (conversation *Conversation) hasContextCheckpoint(native protocol.Context, head string, control json.RawMessage) bool {
	if native == nil {
		return head == "" && len(control) == 0
	}
	checkpoint, ok := conversation.storedContexts[native]
	if !ok || checkpoint.Head != head || checkpoint.Count != native.RecordCount() {
		return false
	}
	currentControl, err := conversation.contextControl(native)
	return err == nil && sameContextControl(currentControl, control)
}

func (conversation *Conversation) cloneCheckpointedContext(source *Conversation, native protocol.Context) protocol.Context {
	if native == nil {
		return nil
	}
	clone := native.Clone()
	conversation.storedContexts[clone] = source.storedContexts[native]
	conversation.executionSnapshots[clone] = source.executionSnapshots[native]
	return clone
}

func sameContextControl(left, right json.RawMessage) bool {
	return bytes.Equal(left, right) || sameJSONValue(left, right)
}

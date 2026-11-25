package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"maps"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	samplerpb "github.com/agentydragon/ducktape/agentplane/sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/jsonwire"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func (s *Store) Begin(ctx context.Context, owner Owner, request IntentPayload, preparation *providerrequest.Preparation, policy *ProviderAttemptPolicy) (string, []byte, error) {
	return s.begin(ctx, owner, request, requestPreparationFor(preparation), policy)
}

func (s *Store) begin(ctx context.Context, owner Owner, request IntentPayload, prepare requestPreparation, policy *ProviderAttemptPolicy) (string, []byte, error) {
	kind := request.Kind
	if !kind.valid() || kind == PendingIntentCompaction {
		return "", nil, protocol.ErrInvalid
	}
	if kind == PendingIntentSampling {
		if policy == nil || policy.validate() != nil {
			return "", nil, protocol.ErrInvalid
		}
	} else if policy != nil {
		return "", nil, protocol.ErrInvalid
	}
	id := uuid.NewString()
	var wire []byte
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if err := checkIntentStart(ctx, tx, owner, conversation); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		wire, err = s.beginIntent(ctx, tx, owner, conversation, session, id, request, prepare, policy)
		return err
	})
	if err != nil {
		return "", nil, err
	}
	return id, wire, nil
}

func checkIntentStart(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation) error {
	if err := checkInterrupt(ctx, tx, owner); err != nil {
		return err
	}
	if conversation.Pending != nil {
		return &protocol.UnknownEffectError{Reason: fmt.Sprintf("unresolved %s intent %s; automatic replay is unsafe", conversation.Pending.Kind, conversation.Pending.ID)}
	}
	return nil
}

func (s *Store) beginIntent(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation, session *Session,
	id string, request IntentPayload, prepare requestPreparation, policy *ProviderAttemptPolicy,
) ([]byte, error) {
	var wire []byte
	if request.ProviderRequest != nil {
		if prepare == nil || request.API != conversation.API {
			return nil, protocol.ErrInvalid
		}
		prepared, rendered, err := prepare(ctx, tx, newContentDB(ctx, tx), conversation, *request.ProviderRequest)
		if err != nil {
			return nil, err
		}
		if policy != nil {
			prepared.SourceNativeContextID = conversation.nativeContextID()
			prepared.SourceControl, err = conversation.contextControl(conversation.Context())
			if err != nil {
				return nil, err
			}
		}
		request.ProviderRequest, wire = &prepared, rendered
	}
	requestWire, err := marshal(request)
	if err != nil {
		return nil, err
	}
	if policy != nil {
		if err := insertProviderAttempt(ctx, tx, id, owner, requestWire, session, *policy, id, nil, 1, policy.Budget); err != nil {
			return nil, err
		}
	} else if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,$3,$4,NULL)`,
		id, owner.ConversationID, owner.Generation, requestWire); err != nil {
		return nil, err
	}
	return wire, s.publishIntent(ctx, tx, conversation, session, id, request)
}

// publishIntent follows the caller's admission checks and intent insertion in the same transaction.
func (s *Store) publishIntent(ctx context.Context, tx *sql.Tx, conversation *Conversation, session *Session,
	id string, request IntentPayload,
) error {
	if err := saveProviderRequest(ctx, tx, id, conversation.ID, request.ProviderRequest); err != nil {
		return err
	}
	conversation.Pending = &PendingIntent{ID: id, Kind: request.Kind}
	if err := s.updateConversation(ctx, tx, conversation); err != nil {
		return err
	}
	return s.recordEvidence(ctx, tx, conversation, session, id, IntentKind(request.Kind).progressKind(), eventpb.ExecutionProgress_PHASE_DISPATCH, request.dispatchEvidence(id))
}

func (s *Store) Finish(ctx context.Context, owner Owner, intentID string, result any, records []json.RawMessage, contextTokens *int) error {
	return s.finish(ctx, owner, intentID, result, contextTokens, nil, func(c *Conversation) error {
		return c.appendProviderRecords(records, intentID)
	})
}

func (s *Store) FinishTool(ctx context.Context, owner Owner, intentID string, result map[string]any, call protocol.ToolInvocation, output []protocol.ContentPart) error {
	return s.finish(ctx, owner, intentID, result, nil, nil, func(c *Conversation) error {
		return c.appendToolResult(call, output, protocol.RecordOrigin{Kind: protocol.RecordOriginToolResult, SourceID: intentID})
	})
}

func (s *Store) finish(ctx context.Context, owner Owner, intentID string, result any, contextTokens *int, snapshot *ProviderAttemptSnapshot, appendResult func(*Conversation) error) error {
	resultWire, err := marshal(result)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwnerWithContextRefs(ctx, tx, owner)
		if err != nil {
			return err
		}
		var kind PendingIntentKind
		if snapshot != nil {
			kind = PendingIntentSampling
		}
		if !pendingMatches(conversation.Pending, intentID, kind) {
			return protocol.ErrPrecondition
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if intent.ConversationID != owner.ConversationID || intent.Generation != owner.Generation {
			return &LostOwnershipError{Reason: "intent belongs to another owner generation"}
		}
		if snapshot != nil && intent.Kind != IntentSampling {
			return protocol.ErrPrecondition
		}
		if intent.Kind == IntentSampling || intent.Kind == IntentCompaction {
			if err := checkInterrupt(ctx, tx, owner); err != nil {
				return err
			}
			if err := requireLiveProviderAttempt(ctx, tx, intentID); err != nil {
				return err
			}
		}
		if snapshot == nil || !conversation.reuseNativeContexts(snapshot.conversation) {
			if err := loadConversationItems(ctx, tx, conversation); err != nil {
				return err
			}
		}
		if err := settleIntentResult(ctx, tx, intentID, resultWire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := s.recordEvidence(ctx, tx, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_RESULT, result); err != nil {
			return err
		}
		if err := appendResult(conversation); err != nil {
			return err
		}
		if intent.Kind == IntentSampling {
			conversation.ContextTokens = contextTokens
		}
		conversation.Pending = nil
		return s.updateConversation(ctx, tx, conversation)
	})
}

func (s *Store) Reject(ctx context.Context, owner Owner, intentID string, failure *protocol.KnownFailureError) error {
	if failure == nil {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if !pendingMatches(conversation.Pending, intentID, PendingIntentKind("")) {
			return protocol.ErrPrecondition
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if intent.Kind == IntentSampling || intent.Kind == IntentCompaction {
			if err := requireLiveProviderAttempt(ctx, tx, intentID); err != nil {
				return err
			}
		}
		unresolved, err := hasUnresolvedIntent(ctx, tx, conversation.ID, intentID)
		if err != nil {
			return err
		}
		liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		liveProcess, err := hasLiveProcess(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if unresolved || liveCell || liveProcess {
			result := map[string]any{"status": IntentResultRejected, "not_dispatched": []any{}}
			maps.Copy(result, failure.Evidence)
			wire, err := marshal(result)
			if err != nil {
				return err
			}
			if err := settleIntentResult(ctx, tx, intentID, wire); err != nil {
				return err
			}
			conversation.Stopped = true
			if err := s.updateConversation(ctx, tx, conversation); err != nil {
				return err
			}
			session, err := sessionForConversation(ctx, tx, conversation.ID)
			if err != nil {
				return err
			}
			if err := s.recordEvidence(ctx, tx, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_REJECTED, result); err != nil {
				return err
			}
			return s.settleFailure(ctx, tx, conversation, session, session.ActiveCommand, failure)
		}
		outputs, err := settlePendingCalls(conversation, func(call protocol.ToolInvocation) string { return "Not dispatched because the turn failed." })
		if err != nil {
			return err
		}
		result := map[string]any{"status": IntentResultRejected, "not_dispatched": outputs}
		maps.Copy(result, failure.Evidence)
		wire, err := marshal(result)
		if err != nil {
			return err
		}
		if err := settleIntentResult(ctx, tx, intentID, wire); err != nil {
			return err
		}

		conversation.Stopped = true
		conversation.Pending = nil
		if err := s.updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := s.recordEvidence(ctx, tx, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_REJECTED, result); err != nil {
			return err
		}
		return s.settleFailure(ctx, tx, conversation, session, session.ActiveCommand, failure)
	})
}

func (s *Store) BeginCompaction(ctx context.Context, owner Owner, request IntentPayload, original protocol.Context, preparation *providerrequest.Preparation, policy ProviderAttemptPolicy) (string, []byte, error) {
	return s.beginCompaction(ctx, owner, request, original, requestPreparationFor(preparation), policy)
}

func (s *Store) beginCompaction(ctx context.Context, owner Owner, request IntentPayload, original protocol.Context, prepare requestPreparation, policy ProviderAttemptPolicy) (string, []byte, error) {
	if request.Kind != PendingIntentCompaction || policy.validate() != nil {
		return "", nil, protocol.ErrInvalid
	}
	id := uuid.NewString()
	var wire []byte
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending != nil {
			return &protocol.UnknownEffectError{Reason: "unresolved intent; cannot compact"}
		}
		if err := checkInterrupt(ctx, tx, owner); err != nil {
			return err
		}
		if !sameCompactionSource(conversation.Context(), original) {
			return protocol.ErrPrecondition
		}
		if err := conversation.Transcript.CheckSettled(); err != nil {
			return err
		}
		if err := conversation.Context().CheckSettled(); err != nil {
			return err
		}
		blocking, err := hasCompactionBlockingWork(ctx, tx, owner)
		if err != nil {
			return err
		}
		if blocking {
			return &protocol.UnknownEffectError{Reason: "unresolved execution work blocks compaction"}
		}
		if request.ProviderRequest != nil {
			if prepare == nil || request.API != conversation.API {
				return protocol.ErrInvalid
			}
			prepared, rendered, err := prepare(ctx, tx, newContentDB(ctx, tx), conversation, *request.ProviderRequest)
			if err != nil {
				return err
			}
			request.ProviderRequest = &prepared
			wire = rendered
		}
		if request.ProviderRequest != nil {
			request.ProviderRequest.SourceNativeContextID = conversation.nativeContextID()
			request.ProviderRequest.SourceControl, err = conversation.contextControl(conversation.Context())
			if err != nil {
				return err
			}
		}
		requestWire, err := marshal(request)
		if err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := insertProviderAttempt(ctx, tx, id, owner, requestWire, session, policy, id, nil, 1, policy.Budget); err != nil {
			return err
		}
		return s.publishIntent(ctx, tx, conversation, session, id, request)
	})
	if err != nil {
		return "", nil, err
	}
	return id, wire, nil
}

func (s *Store) FinishCompaction(ctx context.Context, owner Owner, intentID string, audit any, original, replacement protocol.Context, validateReplacement func(protocol.Context) error) error {
	resultWire, err := marshal(audit)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if !pendingMatches(conversation.Pending, intentID, PendingIntentCompaction) {
			return protocol.ErrPrecondition
		}
		if !sameCompactionSource(conversation.Context(), original) {
			return protocol.ErrPrecondition
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if intent.ConversationID != owner.ConversationID || intent.Generation != owner.Generation {
			return &LostOwnershipError{Reason: "intent belongs to another owner generation"}
		}
		if err := checkInterrupt(ctx, tx, owner); err != nil {
			return err
		}
		if err := requireLiveProviderAttempt(ctx, tx, intentID); err != nil {
			return err
		}
		if err := settleIntentResult(ctx, tx, intentID, resultWire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		sources, err := s.recordEvidenceWithSources(ctx, tx, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_RESULT, audit)
		if err != nil {
			return err
		}
		if replacement.API() != conversation.API {
			return protocol.ErrInvalid
		}
		if err := replacement.CheckSettled(); err != nil {
			return err
		}
		// Persistence can retry the same validated replacement after an ambiguous
		// commit. Keep any appended notifications private to this transaction.
		conversation.ActiveContext = replacement.Clone()
		conversation.activeContextID = ""

		conversation.ContextTokens = nil
		conversation.Pending = nil
		objects := newContentDB(ctx, tx)
		if err := s.recordEnvironmentContext(objects, conversation, session, true); err != nil {
			return err
		}
		if err := objects.Save(); err != nil {
			return err
		}
		if validateReplacement != nil {
			if err := validateReplacement(conversation.ActiveContext); err != nil {
				return err
			}
		}
		if _, err := s.settleNotifications(ctx, tx, conversation, session, true); err != nil {
			return err
		}
		// Compaction can discard the outputs that introduced live handles. Capture
		// their current state under the same owner fence as the replacement.
		state, err := readExecutionState(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		snapshotObjects := newContentDB(ctx, tx)
		if err := s.appendExecutionSnapshot(snapshotObjects, conversation, conversation.ActiveContext, state); err != nil {
			return err
		}
		if err := snapshotObjects.Save(); err != nil {
			return err
		}
		if err := s.updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return s.completeCompactionCommand(ctx, tx, session, sources)
	})
}

func (s *Store) completeCompactionCommand(ctx context.Context, tx *sql.Tx, session *Session, sources []uint64) error {
	if session.ActiveCommand == nil {
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
	payload := &commandpb.Command{}
	if err := proto.Unmarshal(command.Payload, payload); err != nil {
		return err
	}
	if payload.GetCompact() == nil {
		return nil
	}
	if err := setCommandOutcome(ctx, tx, command.SessionID, command.ID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED, nil); err != nil {
		return err
	}
	session.ActiveCommand = nil
	if _, err := s.appendEvent(ctx, tx, session, &eventpb.Event{
		SourceSequences: sources,
		Observation: &eventpb.Event_ExecutionProgress{ExecutionProgress: &eventpb.ExecutionProgress{
			OperationId: command.ID, Kind: eventpb.ExecutionProgress_KIND_COMMAND, Phase: eventpb.ExecutionProgress_PHASE_COMPLETED,
		}},
	}); err != nil {
		return err
	}
	return persistSession(ctx, tx, session)
}

// RecordNested records a nested effect's opaque protocol request under its kind.
func (s *Store) RecordNested(ctx context.Context, owner Owner, kind IntentKind, request map[string]any) (string, error) {
	id := uuid.NewString()
	if kind != IntentTool && kind != IntentExecutor && kind != IntentInterruptProcess {
		return "", protocol.ErrInvalid
	}
	request["kind"] = kind
	requestWire, err := marshal(request)
	if err != nil {
		return "", err
	}
	err = s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if kind == IntentTool {
			if err := checkInterrupt(ctx, tx, owner); err != nil {
				return err
			}
		}
		if pendingMatches(conversation.Pending, "", PendingIntentCompaction) {
			live, err := hasLiveCell(ctx, tx, owner.ConversationID)
			if err != nil {
				return err
			}
			if kind == IntentInterruptProcess || !live {
				return &protocol.UnknownEffectError{Reason: "cannot start this nested effect during compaction"}
			}
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,$3,$4,NULL)`, id, owner.ConversationID, owner.Generation, requestWire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, conversation, session, id, kind.progressKind(), eventpb.ExecutionProgress_PHASE_DISPATCH, request)
	})
	return id, err
}

func (s *Store) FinishNested(ctx context.Context, owner Owner, intentID string, result any) error {
	wire, err := marshal(result)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if intent.Generation != owner.Generation || intent.ConversationID != owner.ConversationID {
			return &LostOwnershipError{Reason: "nested intent belongs to another owner generation"}
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, wire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_RESULT, result)
	})
}

func pendingMatches(pending *PendingIntent, id string, kind PendingIntentKind) bool {
	if pending == nil {
		return false
	}
	if id != "" && pending.ID != id {
		return false
	}
	return kind == "" || pending.Kind == kind
}

func sameCompactionSource(left, right protocol.Context) bool {
	if left.RecordCount() != right.RecordCount() {
		return false
	}
	for i := range left.RecordCount() {
		if left.OriginAt(i) != right.OriginAt(i) {
			return false
		}
	}
	return sameNativeContext(left, right)
}

func sameNativeContext(left, right protocol.Context) bool {
	if left.API() != right.API() {
		return false
	}
	l, err := json.Marshal(left)
	if err != nil {
		return false
	}
	r, err := json.Marshal(right)
	if err != nil {
		return false
	}
	lv, err := jsonwire.DecodeJSONValue(l)
	if err != nil {
		return false
	}
	rv, err := jsonwire.DecodeJSONValue(r)
	if err != nil {
		return false
	}
	lc, err := json.Marshal(lv)
	if err != nil {
		return false
	}
	rc, err := json.Marshal(rv)
	return err == nil && bytes.Equal(lc, rc)
}

func intentFor(ctx context.Context, tx *sql.Tx, id string) (*Intent, error) {
	var intent Intent
	var request []byte
	err := tx.QueryRowContext(ctx, `SELECT id::text,conversation_id::text,generation,request,result FROM sampler_intent WHERE id=$1`, id).Scan(
		&intent.ID, &intent.ConversationID, &intent.Generation, &request, &intent.ResultWire,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, protocol.ErrMissing
	}
	if err != nil {
		return nil, err
	}
	decoded, err := decodeIntentPayload(request)
	if err != nil {
		return nil, err
	}
	intent.Kind, intent.CallID = decoded.Kind, decoded.callID()
	return &intent, nil
}

// storedIntentPayload is the subset of an intent's stored input the store reads, whatever its kind.
type storedIntentPayload struct {
	Kind IntentKind `json:"kind"`
	Call *struct {
		CallID string `json:"call_id"`
	} `json:"call"`
}

func (request storedIntentPayload) callID() string {
	if request.Call == nil {
		return ""
	}
	return request.Call.CallID
}

func decodeIntentPayload(raw []byte) (storedIntentPayload, error) {
	var request storedIntentPayload
	if err := json.Unmarshal(raw, &request); err != nil {
		return request, fmt.Errorf("decode stored intent payload: %w", err)
	}
	return request, nil
}

func hasUnresolvedIntent(ctx context.Context, tx *sql.Tx, conversationID, exceptID string) (bool, error) {
	var found bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL AND ($2='' OR id<>$2::uuid))`, conversationID, exceptID).Scan(&found)
	return found, err
}

func hasCompactionBlockingWork(ctx context.Context, tx *sql.Tx, owner Owner) (bool, error) {
	// A live cell under this owner can finish its tool and executor intents
	// independently of the provider request. Older work may have lost its host.
	var blocking bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_cell
		WHERE conversation_id=$1 AND generation<>$2 AND status='live')
		OR EXISTS(SELECT 1 FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL
			AND (generation<>$2 OR COALESCE(request->>'kind','') NOT IN ('tool','executor')
				OR NOT EXISTS (SELECT 1 FROM sampler_cell WHERE conversation_id=$1 AND generation=$2 AND status='live')))`,
		owner.ConversationID, owner.Generation).Scan(&blocking)
	return blocking, err
}

func settlePendingCalls(conversation *Conversation, message func(protocol.ToolInvocation) string) ([]map[string]any, error) {
	calls, err := conversation.Transcript.PendingCalls()
	if err != nil {
		return nil, err
	}
	outputs := make([]map[string]any, 0, len(calls))
	for _, call := range calls {
		content := []protocol.ContentPart{{Type: "input_text", Text: message(call)}}
		if err := conversation.appendToolResult(call, content, protocol.RecordOrigin{}); err != nil {
			return nil, err
		}
		outputs = append(outputs, map[string]any{"call_id": call.CallID, "output": content})
	}
	return outputs, nil
}

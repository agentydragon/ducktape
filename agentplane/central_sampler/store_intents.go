package centralsampler

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"maps"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func (s *Store) Begin(ctx context.Context, owner Owner, request IntentRequest, prepare requestPreparation) (string, []byte, error) {
	kind := request.Kind
	if !kind.valid() {
		return "", nil, protocol.ErrInvalid
	}
	id := uuid.NewString()
	var wire []byte
	var conversation *Conversation
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var err error
		conversation, err = lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if err := checkInterrupt(ctx, tx, owner); err != nil {
			return err
		}
		if conversation.Pending != nil {
			return &protocol.UnknownEffectError{Reason: fmt.Sprintf("unresolved %s intent %s; automatic replay is unsafe", conversation.Pending.Kind, conversation.Pending.ID)}
		}
		if request.ProviderRequest != nil {
			if prepare == nil || request.API != conversation.API {
				return protocol.ErrInvalid
			}
			prepared, rendered, err := prepare(newContentStore(ctx, tx), conversation, *request.ProviderRequest)
			if err != nil {
				return err
			}
			request.ProviderRequest = &prepared
			wire = rendered
		}
		requestWire, err := marshal(request)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,$3,$4,NULL)`,
			id, owner.ConversationID, owner.Generation, requestWire); err != nil {
			return err
		}
		if err := saveProviderRequest(ctx, tx, id, conversation.ID, request.ProviderRequest); err != nil {
			return err
		}
		conversation.Pending = &PendingIntent{ID: id, Kind: kind}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, id, IntentKind(kind).progressKind(), eventpb.ExecutionProgress_PHASE_DISPATCH, request.dispatchEvidence(id))
	})
	if err != nil {
		return "", nil, err
	}
	return id, wire, nil
}

func (s *Store) Finish(ctx context.Context, owner Owner, intentID string, result any, records []json.RawMessage, contextTokens *int) error {
	return s.finish(ctx, owner, intentID, result, contextTokens, func(c *Conversation) error {
		return c.apply(func(state protocol.Context) error { return state.AppendRecords(records) })
	})
}

func (s *Store) FinishTool(ctx context.Context, owner Owner, intentID string, result map[string]any, call protocol.ToolInvocation, output []protocol.ContentPart) error {
	return s.finish(ctx, owner, intentID, result, nil, func(c *Conversation) error {
		return c.appendToolResult(call, output)
	})
}

func (s *Store) finish(ctx context.Context, owner Owner, intentID string, result any, contextTokens *int, appendResult func(*Conversation) error) error {
	resultWire, err := marshal(result)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
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
		if intent.ConversationID != owner.ConversationID || intent.Generation != owner.Generation {
			return &LostOwnershipError{Reason: "intent belongs to another owner generation"}
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, resultWire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := s.recordEvidence(ctx, tx, owner, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_RESULT, result); err != nil {
			return err
		}
		if err := appendResult(conversation); err != nil {
			return err
		}
		if intent.Kind == IntentSampling {
			conversation.ContextTokens = contextTokens
		}
		conversation.Pending = nil
		return updateConversation(ctx, tx, conversation)
	})
}

func (s *Store) Reject(ctx context.Context, owner Owner, intentID string, failure *protocol.KnownFailureError) error {
	if failure == nil {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
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
			if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, wire); err != nil {
				return err
			}
			conversation.Stopped = true
			if err := updateConversation(ctx, tx, conversation); err != nil {
				return err
			}
			session, err := sessionForConversation(ctx, tx, conversation.ID)
			if err != nil {
				return err
			}
			if err := s.recordEvidence(ctx, tx, owner, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_REJECTED, result); err != nil {
				return err
			}
			return s.settleFailure(ctx, tx, owner, conversation, session, session.ActiveCommand, failure)
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
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, wire); err != nil {
			return err
		}

		conversation.Stopped = true
		conversation.Pending = nil
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if err := s.recordEvidence(ctx, tx, owner, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_REJECTED, result); err != nil {
			return err
		}
		return s.settleFailure(ctx, tx, owner, conversation, session, session.ActiveCommand, failure)
	})
}

func (s *Store) BeginCompaction(ctx context.Context, owner Owner, request IntentRequest, original protocol.Context, prepare requestPreparation) (string, []byte, error) {
	if request.Kind != PendingIntentCompaction {
		return "", nil, protocol.ErrInvalid
	}
	id := uuid.NewString()
	var wire []byte
	err := s.transaction(ctx, func(tx *sql.Tx) error {
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
		if !sameNativeContext(conversation.Context(), original) {
			return protocol.ErrPrecondition
		}
		liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if liveCell {
			return &protocol.UnknownEffectError{Reason: "cannot compact over live JavaScript cells"}
		}
		unresolved, err := hasUnresolvedIntent(ctx, tx, conversation.ID, "")
		if err != nil {
			return err
		}
		if unresolved {
			return &protocol.UnknownEffectError{Reason: "unresolved nested intent; cannot compact"}
		}
		if request.ProviderRequest != nil {
			if prepare == nil || request.API != conversation.API {
				return protocol.ErrInvalid
			}
			prepared, rendered, err := prepare(newContentStore(ctx, tx), conversation, *request.ProviderRequest)
			if err != nil {
				return err
			}
			request.ProviderRequest = &prepared
			wire = rendered
		}
		requestWire, err := marshal(request)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,$3,$4,NULL)`, id, owner.ConversationID, owner.Generation, requestWire); err != nil {
			return err
		}
		if err := saveProviderRequest(ctx, tx, id, conversation.ID, request.ProviderRequest); err != nil {
			return err
		}
		conversation.Pending = &PendingIntent{ID: id, Kind: PendingIntentCompaction}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, id, eventpb.ExecutionProgress_KIND_COMPACTION, eventpb.ExecutionProgress_PHASE_DISPATCH, request.dispatchEvidence(id))
	})
	if err != nil {
		return "", nil, err
	}
	return id, wire, nil
}

func (s *Store) FinishCompaction(ctx context.Context, owner Owner, intentID string, audit any, original, replacement protocol.Context) error {
	resultWire, err := marshal(audit)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if !pendingMatches(conversation.Pending, intentID, PendingIntentCompaction) {
			return protocol.ErrPrecondition
		}
		if !sameNativeContext(conversation.Context(), original) {
			return protocol.ErrPrecondition
		}
		intent, err := intentFor(ctx, tx, intentID)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, resultWire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		sources, err := s.recordEvidenceWithSources(ctx, tx, owner, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_RESULT, audit)
		if err != nil {
			return err
		}
		if replacement.API() != conversation.API {
			return protocol.ErrInvalid
		}
		if err := replacement.CheckSettled(); err != nil {
			return err
		}
		environment, err := s.environmentContext(conversation, session)
		if err != nil {
			return err
		}
		conversation.EnvironmentContext = &environment
		conversation.ActiveContext = replacement
		conversation.activeContextID = ""

		conversation.ContextTokens = nil
		conversation.Pending = nil
		if err := updateConversation(ctx, tx, conversation); err != nil {
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
	if _, err := appendEvent(ctx, tx, session, &eventpb.Event{
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
	err = s.transaction(ctx, func(tx *sql.Tx) error {
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
			return &protocol.UnknownEffectError{Reason: "cannot start a nested effect during compaction"}
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,$3,$4,NULL)`, id, owner.ConversationID, owner.Generation, requestWire); err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, id, kind.progressKind(), eventpb.ExecutionProgress_PHASE_DISPATCH, request)
	})
	return id, err
}

func (s *Store) FinishNested(ctx context.Context, owner Owner, intentID string, result any) error {
	wire, err := marshal(result)
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
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
		return s.recordEvidence(ctx, tx, owner, conversation, session, intentID, intent.Kind.progressKind(), eventpb.ExecutionProgress_PHASE_RESULT, result)
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
	lv, err := protocol.DecodeJSONValue(l)
	if err != nil {
		return false
	}
	rv, err := protocol.DecodeJSONValue(r)
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
	decoded, err := decodeIntentRequest(request)
	if err != nil {
		return nil, err
	}
	intent.Kind, intent.CallID = decoded.Kind, decoded.callID()
	return &intent, nil
}

// storedIntentRequest is the part of a stored intent request the store reads, whatever its kind.
type storedIntentRequest struct {
	Kind IntentKind `json:"kind"`
	Call *struct {
		CallID string `json:"call_id"`
	} `json:"call"`
}

func (request storedIntentRequest) callID() string {
	if request.Call == nil {
		return ""
	}
	return request.Call.CallID
}

func decodeIntentRequest(raw []byte) (storedIntentRequest, error) {
	var request storedIntentRequest
	if err := json.Unmarshal(raw, &request); err != nil {
		return request, fmt.Errorf("decode stored intent request: %w", err)
	}
	return request, nil
}

func hasUnresolvedIntent(ctx context.Context, tx *sql.Tx, conversationID, exceptID string) (bool, error) {
	var found bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL AND ($2='' OR id<>$2::uuid))`, conversationID, exceptID).Scan(&found)
	return found, err
}

func settlePendingCalls(conversation *Conversation, message func(protocol.ToolInvocation) string) ([]map[string]any, error) {
	calls, err := conversation.Transcript.PendingCalls()
	if err != nil {
		return nil, err
	}
	outputs := make([]map[string]any, 0, len(calls))
	for _, call := range calls {
		content := []protocol.ContentPart{{Type: "input_text", Text: message(call)}}
		if err := conversation.appendToolResult(call, content); err != nil {
			return nil, err
		}
		outputs = append(outputs, map[string]any{"call_id": call.CallID, "output": content})
	}
	return outputs, nil
}

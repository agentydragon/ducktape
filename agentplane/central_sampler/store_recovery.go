package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

const (
	codeHostLossNotice       = "The JavaScript host that held this Session's store()/load() values has been lost. Prior values and live-cell handles are unavailable. No missing operation was replayed. Recreate required JavaScript state in a new exec cell before relying on it."
	codeHostUndispatchedCall = "The JavaScript host was lost before this call was dispatched. No JavaScript cell or executor operation was started for this call. Prior store()/load() values are unavailable; recreate required state in a new exec cell."
)

// lostIntent is an unresolved intent that no owner can finish any more.
type lostIntent struct {
	ID         string
	Generation int
	Kind       IntentKind
	// Operation names what was dispatched: the nested tool name, the executor method, or the
	// model call ID of a cell.
	Operation string
}

func (intent lostIntent) describe() string {
	if intent.Operation == "" {
		return fmt.Sprintf("%s intent %s", intent.Kind, intent.ID)
	}
	return fmt.Sprintf("%s %s (intent %s)", intent.Kind, intent.Operation, intent.ID)
}

// lostIntents lists the unresolved intents, oldest first, with what each dispatched.
func lostIntents(ctx context.Context, tx *sql.Tx, conversationID string) ([]lostIntent, error) {
	rows, err := tx.QueryContext(ctx, `SELECT id::text,generation,request,request->'tool_name'->>'name',request->>'method' FROM sampler_intent
		WHERE conversation_id=$1 AND result IS NULL ORDER BY id FOR UPDATE`, conversationID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var intents []lostIntent
	for rows.Next() {
		var intent lostIntent
		var requestWire []byte
		var toolName, method sql.NullString
		if err := rows.Scan(&intent.ID, &intent.Generation, &requestWire, &toolName, &method); err != nil {
			return nil, err
		}
		request, err := decodeIntentRequest(requestWire)
		if err != nil {
			return nil, err
		}
		intent.Kind = request.Kind
		switch {
		case toolName.Valid:
			intent.Operation = toolName.String
		case method.Valid:
			intent.Operation = method.String
		default:
			intent.Operation = request.callID()
		}
		intents = append(intents, intent)
	}
	return intents, rows.Err()
}

func lostCellCallMessage(nested []lostIntent) string {
	message := "The JavaScript host was lost while this cell was running. The cell did not finish and its handle is gone."
	if len(nested) > 0 {
		names := make([]string, 0, len(nested))
		for _, intent := range nested {
			names = append(names, intent.describe())
		}
		message += " Calls it dispatched whose outcome is unknown: " + strings.Join(names, ", ") + "."
	}
	return message + " Nothing was replayed; remote effects may have occurred. Prior store()/load() values and live-cell handles are unavailable; recreate required state in a new exec cell."
}

// ReconcileCodeHostLoss runs before any next model step. When the recorded JavaScript host is
// not this runtime's, or an older owner generation left an intent or cell unresolved, it settles
// that work under the current owner fence instead of blocking: every unresolved intent is
// abandoned as an unknown remote outcome, live cells and process handles are abandoned with their
// retained cursor and output, pending model tool calls get truthful outputs paired to their exact
// call IDs, the JavaScript epoch advances and a model-visible notice is appended. Nothing is
// replayed; the turn then continues so the model decides. A Session blocked on this state is
// unblocked in the same transaction. With nothing lost, the stale host marker is cleared with the
// notice as before; unrelated (non-model) tool calls stay with their ordinary dispatcher and the
// notice follows once they settle.
func (s *Store) ReconcileCodeHostLoss(ctx context.Context, owner Owner, instanceID string, encoding protocol.ExecEncoding) (bool, error) {
	if _, err := uuid.Parse(instanceID); err != nil {
		return false, protocol.ErrInvalid
	}
	changed := false
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		var previous sql.NullString
		if err := tx.QueryRowContext(ctx, `SELECT code_host_instance::text FROM sampler_conversation WHERE id=$1`, conversation.ID).Scan(&previous); err != nil {
			return err
		}
		hostLost := previous.Valid && previous.String != instanceID
		// An older generation is fenced out, so its unresolved intents and live cells can never
		// settle on their own; a same-generation host failure likewise orphans them.
		var staleWork bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL AND generation<$2)
			OR EXISTS(SELECT 1 FROM sampler_cell WHERE conversation_id=$1 AND status='live' AND generation<$2)`, conversation.ID, owner.Generation).Scan(&staleWork); err != nil {
			return err
		}
		if !hostLost && !staleWork {
			return nil
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		// A block an owner recorded on the host that still serves the Session is a reviewed
		// decision (an uncommitted provider stream, an interrupt with unconfirmed handles); only
		// a lost host or lost owner settles it without an operator. See the
		// TODO(SAMPLER_INTENT_LEASES) in settleFailure for the design that retires that block.
		if !hostLost && session.Blocked {
			return nil
		}
		intents, err := lostIntents(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		var reconciliation *eventpb.Event
		if conversation.Pending != nil && conversation.Pending.Kind == PendingIntentSampling {
			reconciliation, err = samplingReconciliation(ctx, tx, session, conversation.Pending.ID)
			if err != nil {
				return err
			}
		}
		abandonedIntents := make([]map[string]any, 0, len(intents))
		var nested []lostIntent
		lostCellCalls := map[string]bool{}
		for _, intent := range intents {
			var linked *eventpb.Event
			if intent.Kind == IntentSampling && conversation.Pending != nil && intent.ID == conversation.Pending.ID {
				linked = reconciliation
			}
			if err := s.abandonIntent(ctx, tx, owner, conversation, session, intent.ID, intent.Kind, linked); err != nil {
				return err
			}
			switch intent.Kind {
			case IntentTool, IntentExecutor:
				nested = append(nested, intent)
			case IntentCell:
				lostCellCalls[intent.Operation] = true
			}
			abandonedIntents = append(abandonedIntents, map[string]any{"id": intent.ID, "kind": intent.Kind, "operation": intent.Operation, "generation": intent.Generation})
		}
		cellRows, err := tx.QueryContext(ctx, `UPDATE sampler_cell SET status='abandoned' WHERE conversation_id=$1 AND status='live' RETURNING id`, conversation.ID)
		if err != nil {
			return err
		}
		var abandonedCells []string
		for cellRows.Next() {
			var id string
			if err := cellRows.Scan(&id); err != nil {
				cellRows.Close()
				return err
			}
			abandonedCells = append(abandonedCells, id)
		}
		if err := cellRows.Err(); err != nil {
			cellRows.Close()
			return err
		}
		cellRows.Close()
		for _, id := range abandonedCells {
			if err := s.recordEvidence(ctx, tx, owner, conversation, session, id, eventpb.ExecutionProgress_KIND_CELL, eventpb.ExecutionProgress_PHASE_ABANDONED, map[string]any{}); err != nil {
				return err
			}
		}
		processRows, err := tx.QueryContext(ctx, `UPDATE sampler_process SET status='abandoned'
			WHERE conversation_id=$1 AND status NOT IN ('exited','exited_unread','abandoned') RETURNING `+processColumns, conversation.ID)
		if err != nil {
			return err
		}
		var abandonedProcesses []map[string]any
		for processRows.Next() {
			process, err := scanProcess(processRows)
			if err != nil {
				processRows.Close()
				return err
			}
			abandonedProcesses = append(abandonedProcesses, map[string]any{
				"process_id": process.ID, "after_seq": process.AfterSequence, "exit_code": process.ExitCode, "remote_outcome": NestedUnknown,
				"automatic_replay": false, "buffered_bytes": len(process.BufferedOutput), "omitted_bytes": process.OmittedBytes,
			})
		}
		if err := processRows.Err(); err != nil {
			processRows.Close()
			return err
		}
		processRows.Close()
		for _, evidence := range abandonedProcesses {
			if err := s.recordEvidence(ctx, tx, owner, conversation, session, evidence["process_id"].(string), eventpb.ExecutionProgress_KIND_PROCESS, eventpb.ExecutionProgress_PHASE_ABANDONED, evidence); err != nil {
				return err
			}
		}
		settled := len(intents) > 0 || len(abandonedCells) > 0 || len(abandonedProcesses) > 0
		conversation.Pending = nil

		calls, err := conversation.Transcript.PendingCalls()
		if err != nil {
			return err
		}
		// Leave unrelated tool calls to their normal dispatcher. Failed volatile calls already
		// carry the loss message in their exact paired output.
		for _, call := range calls {
			if !call.IsModelTool(encoding) {
				break
			}
			text := codeHostUndispatchedCall
			if lostCellCalls[call.CallID] {
				text = lostCellCallMessage(nested)
			}
			output := []protocol.ContentPart{{Type: "input_text", Text: text}}
			if err := conversation.appendToolResult(call, output); err != nil {
				return err
			}
			encoded, err := json.Marshal(output)
			if err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
				ItemId: call.CallID, Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: protocol.DisplayText(string(encoded)), Succeeded: false}},
			}}}); err != nil {
				return err
			}
			changed = true
		}
		remaining, err := conversation.Transcript.PendingCalls()
		if err != nil {
			return err
		}
		// With nothing settled, the notice waits for unrelated calls so it precedes the next
		// sample; settled work resets the JavaScript session now, so its notice cannot wait.
		if len(remaining) == 0 || settled {
			notice := codeHostLossNotice
			if len(nested) > 0 {
				names := make([]string, 0, len(nested))
				for _, intent := range nested {
					names = append(names, intent.describe())
				}
				notice += " Calls abandoned with unknown outcome: " + strings.Join(names, ", ") + "."
			}
			if err := conversation.apply(func(state protocol.Context) error {
				return state.AppendNotice([]protocol.ContentPart{{Type: "input_text", Text: notice}})
			}); err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET code_session_epoch=code_session_epoch+1,code_host_instance=NULL WHERE id=$1`, conversation.ID); err != nil {
				return err
			}
			if err := s.recordEvidence(ctx, tx, owner, conversation, session, uuid.NewString(), eventpb.ExecutionProgress_KIND_CODE_HOST_LOSS, eventpb.ExecutionProgress_PHASE_COMMITTED, map[string]any{
				"previous_instance": previous.String, "next_instance": instanceID, "host_lost": hostLost, "stale_generation_work": staleWork,
				"javascript_state_reset": true, "automatic_replay": false, "remote_outcome": NestedUnknown,
				"abandoned_intents": abandonedIntents, "abandoned_cells": abandonedCells, "abandoned_processes": abandonedProcesses,
				"unblocked": session.Blocked,
			}); err != nil {
				return err
			}
			changed = true
		}
		if session.Blocked {
			session.Blocked, session.BlockedReason = false, nil
			if err := persistSession(ctx, tx, session); err != nil {
				return err
			}
			changed = true
		}
		if settled {
			slog.Info("central sampler settled lost JavaScript host work", "session_id", session.ID, "conversation_id", conversation.ID,
				"generation", owner.Generation, "host_lost", hostLost, "stale_generation_work", staleWork, "abandoned_intents", len(intents),
				"abandoned_cells", len(abandonedCells), "abandoned_processes", len(abandonedProcesses), "previous_instance", previous.String, "instance", instanceID)
		}
		if changed {
			return updateConversation(ctx, tx, conversation)
		}
		return nil
	})
	return changed, err
}

// abandonIntent settles one unresolved intent as an unknown remote outcome that is never replayed
// and records the abandonment; a sampling reconciliation, when given, is appended linked to it.
func (s *Store) abandonIntent(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation, session *Session,
	intentID string, kind IntentKind, reconciliation *eventpb.Event,
) error {
	abandoned := map[string]any{"status": IntentResultAbandoned, "remote_outcome": NestedUnknown, "automatic_replay": false}
	if kind == IntentSampling || kind == IntentCompaction {
		abandoned["provider_completion_unknown"] = true
		abandoned["output_committed"] = false
	}
	wire, err := marshal(abandoned)
	if err != nil {
		return err
	}
	result, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1 AND result IS NULL`, intentID, wire)
	if err != nil {
		return err
	}
	updated, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if updated != 1 {
		return protocol.ErrPrecondition
	}
	sources, err := s.recordEvidenceWithSources(ctx, tx, owner, conversation, session, intentID, kind.progressKind(), eventpb.ExecutionProgress_PHASE_ABANDONED, abandoned)
	if err != nil {
		return err
	}
	if reconciliation != nil {
		reconciliation.SourceSequences = append(reconciliation.SourceSequences, sources...)
		if _, err := appendEvent(ctx, tx, session, reconciliation); err != nil {
			return err
		}
	}
	return nil
}

// RecoverSession abandons one blocked runtime or sampling boundary after an operator has inspected
// the recorded evidence. It never retries the provider request or any executor operation.
func (s *Store) RecoverSession(ctx context.Context, id, ownerUID string, expectedCursor uint64, operatorNote, replacementIncarnationUID string) (*Session, error) {
	note := strings.TrimSpace(operatorNote)
	if id == "" || ownerUID == "" || note == "" || len(note) > 4096 || len(replacementIncarnationUID) > 128 || strings.TrimSpace(replacementIncarnationUID) != replacementIncarnationUID {
		return nil, protocol.ErrInvalid
	}
	var recovered *Session
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, session, err := lockSession(ctx, tx, id)
		if err != nil {
			return err
		}
		if session.AccountUID != ownerUID {
			return ErrForbidden
		}
		if session.Cursor != expectedCursor || !session.Blocked {
			return protocol.ErrPrecondition
		}
		var activeLease bool
		if err := tx.QueryRowContext(ctx, `SELECT owner IS NOT NULL AND lease_until IS NOT NULL AND lease_until>clock_timestamp()
			FROM sampler_conversation WHERE id=$1`, conversation.ID).Scan(&activeLease); err != nil {
			return err
		}
		if activeLease {
			return protocol.ErrPrecondition
		}
		if session.SetupState != sessionpb.SetupState_SETUP_STATE_NOT_REQUIRED && session.SetupState != sessionpb.SetupState_SETUP_STATE_SUCCEEDED {
			return protocol.ErrPrecondition
		}
		if session.ActiveCommand != nil || session.ActiveTurn != nil {
			return protocol.ErrPrecondition
		}
		if conversation.Pending != nil && conversation.Pending.Kind != PendingIntentCell && conversation.Pending.Kind != PendingIntentSampling {
			return protocol.ErrPrecondition
		}
		pendingSampling := conversation.Pending != nil && conversation.Pending.Kind == PendingIntentSampling

		type unresolvedIntent struct {
			id   string
			kind IntentKind
		}
		rows, err := tx.QueryContext(ctx, `SELECT id::text,request FROM sampler_intent
			WHERE conversation_id=$1 AND result IS NULL ORDER BY id FOR UPDATE`, conversation.ID)
		if err != nil {
			return err
		}
		var unresolved []unresolvedIntent
		pendingIntentFound := conversation.Pending == nil
		for rows.Next() {
			var intentID string
			var requestWire []byte
			if err := rows.Scan(&intentID, &requestWire); err != nil {
				rows.Close()
				return err
			}
			request, err := decodeIntentRequest(requestWire)
			if err != nil {
				rows.Close()
				return err
			}
			kind := request.Kind
			if kind != IntentCell && kind != IntentTool && kind != IntentExecutor && kind != IntentSampling {
				rows.Close()
				return protocol.ErrPrecondition
			}
			if kind == IntentCell || kind == IntentSampling {
				if conversation.Pending == nil || intentID != conversation.Pending.ID ||
					conversation.Pending.Kind != PendingIntentKind(kind) || pendingIntentFound {
					rows.Close()
					return protocol.ErrPrecondition
				}
				pendingIntentFound = true
			}
			unresolved = append(unresolved, unresolvedIntent{id: intentID, kind: kind})
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		if err := rows.Close(); err != nil {
			return err
		}
		rejectedCallID, rejectionReason := "", ""
		if !pendingIntentFound {
			// Older input validation recorded a known rejection but retained Pending
			// when other live work existed. Preserve that result during explicit recovery.
			intent, err := intentFor(ctx, tx, conversation.Pending.ID)
			if err != nil {
				return err
			}
			var rejection RejectedCellResult
			if len(intent.ResultWire) > 0 {
				if err := json.Unmarshal(intent.ResultWire, &rejection); err != nil {
					return err
				}
			}
			if intent.ConversationID != conversation.ID || intent.Kind != IntentCell ||
				rejection.Status != IntentResultRejected || rejection.Dispatched == nil || *rejection.Dispatched || intent.CallID == "" {
				return protocol.ErrPrecondition
			}
			rejectedCallID = intent.CallID
			rejectionReason = rejection.Reason
			if rejectedCallID == "" || rejectionReason == "" {
				return protocol.ErrPrecondition
			}
		}

		liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		liveProcess, err := hasLiveProcess(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if len(unresolved) == 0 && !liveCell && !liveProcess && rejectedCallID == "" {
			return protocol.ErrPrecondition
		}
		var reconciliation *eventpb.Event
		if pendingSampling {
			// Provider output enters model context atomically at Finish. Do not infer that
			// it completed, or that an unrelated live effect is safe.
			if len(unresolved) != 1 || unresolved[0].kind != IntentSampling || liveCell || liveProcess {
				return protocol.ErrPrecondition
			}
			calls, err := conversation.Transcript.PendingCalls()
			if err != nil {
				return err
			}
			if len(calls) != 0 {
				return protocol.ErrPrecondition
			}
			reconciliation, err = samplingReconciliation(ctx, tx, session, conversation.Pending.ID)
			if err != nil {
				return err
			}
		}

		abandonedIntentIDs := make([]string, 0, len(unresolved))
		for _, intent := range unresolved {
			if err := s.abandonIntent(ctx, tx, Owner{ConversationID: conversation.ID, Generation: conversation.Generation}, conversation, session, intent.id, intent.kind, reconciliation); err != nil {
				return err
			}
			abandonedIntentIDs = append(abandonedIntentIDs, intent.id)
		}

		calls, err := conversation.Transcript.PendingCalls()
		if err != nil {
			return err
		}
		for _, call := range calls {
			message := "Aborted by operator recovery; remote effects may have occurred and were not replayed. The continuation uses a new JavaScript session, so prior store() and load() values are unavailable."
			if call.CallID == rejectedCallID {
				message = "Tool call rejected before dispatch: " + rejectionReason + ". No runtime or executor operation was dispatched for this call."
			}
			output := []protocol.ContentPart{{Type: "input_text", Text: message}}
			if err := conversation.appendToolResult(call, output); err != nil {
				return err
			}
			encoded, err := json.Marshal(output)
			if err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
				ItemId: call.CallID, Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: protocol.DisplayText(string(encoded)), Succeeded: false}},
			}}}); err != nil {
				return err
			}
		}
		notice := "An operator recovered this session after reviewing its boundary evidence. Unfinished JavaScript cells and executor operations were abandoned without replay; their remote outcome is unknown. The continuation uses a new JavaScript session, so prior store()/load() values and live-cell handles are unavailable. Recreate any required state in a new exec cell before continuing."
		if pendingSampling {
			notice = "An operator abandoned an interrupted provider response after reviewing its retained request and stream evidence. The provider may have completed or charged for the request; its completion and external effects are unknown. The observed stream prefix was not committed as model output, and no sampler tool call from this response was dispatched. The response will not be replayed. Continue from the last committed conversation after new user input. This recovery starts a fresh JavaScript session; prior store()/load() values are unavailable."
		}
		if err := conversation.apply(func(state protocol.Context) error {
			return state.AppendNotice([]protocol.ContentPart{{Type: "input_text", Text: notice}})
		}); err != nil {
			return err
		}

		if _, err := tx.ExecContext(ctx, `UPDATE sampler_cell SET status='abandoned' WHERE conversation_id=$1 AND status='live'`, conversation.ID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_process SET status='abandoned'
			WHERE conversation_id=$1 AND status NOT IN ('exited','exited_unread','abandoned')`, conversation.ID); err != nil {
			return err
		}

		previousIncarnationUID := ""
		if replacementIncarnationUID != "" {
			configuration := new(samplerpb.CreateSessionRequest)
			if err := proto.Unmarshal(session.Configuration, configuration); err != nil {
				return err
			}
			previousIncarnationUID = configuration.GetExpectedIncarnationUid()
			configuration.ExpectedIncarnationUid = replacementIncarnationUID
			wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(configuration)
			if err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, `UPDATE sampler_session SET configuration=$2 WHERE id=$1`, session.ID, wire); err != nil {
				return err
			}
			session.Configuration = wire
			conversation.ExecutorSession = nil
			conversation.ExecutorShell = nil
		}
		conversation.Pending = nil
		conversation.Stopped = false
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET generation=generation+1,code_session_epoch=code_session_epoch+1,
			owner=NULL,lease_until=NULL WHERE id=$1`, conversation.ID); err != nil {
			return err
		}
		recoveryEvidence := map[string]any{
			"status": "recovered", "operator_note": note, "javascript_state_reset": true,
			"remote_outcome": NestedUnknown, "automatic_replay": false,
			"abandoned_intent_ids": abandonedIntentIDs,
		}
		if pendingSampling {
			recoveryEvidence["provider_completion_unknown"] = true
			recoveryEvidence["output_committed"] = false
		}
		if replacementIncarnationUID != "" {
			recoveryEvidence["previous_incarnation_uid"] = previousIncarnationUID
			recoveryEvidence["replacement_incarnation_uid"] = replacementIncarnationUID
		}
		if err := s.recordEvidence(ctx, tx, Owner{ConversationID: conversation.ID}, conversation, session,
			uuid.NewString(), eventpb.ExecutionProgress_KIND_RECOVERY, eventpb.ExecutionProgress_PHASE_COMMITTED, recoveryEvidence); err != nil {
			return err
		}
		session.Blocked = false
		session.BlockedReason = nil
		if err := persistSession(ctx, tx, session); err != nil {
			return err
		}
		recovered = session
		return nil
	})
	return recovered, err
}

// RetireLostExecutorSession records that a retained exec-server session was definitively absent.
// It abandons process handles without claiming they exited, clears the stale binding, and adds a
// durable model-visible notice before any fresh executor session can be used.
const lostExecutorSessionNotice = "The retained exec-server session was definitively lost. Its live process handles have been abandoned; their remote state is unknown unless explicitly reported, and no mutation was replayed. A fresh executor session is being opened. Recreate any required process state before continuing; old process handles cannot be reused."

func (s *Store) RetireLostExecutorSession(ctx context.Context, owner Owner, sessionID, notDispatchedProcessID string) error {
	if sessionID == "" {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.ExecutorSession == nil || *conversation.ExecutorSession != sessionID {
			return protocol.ErrPrecondition
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.retireExecutorSession(ctx, tx, owner, conversation, session, notDispatchedProcessID, lostExecutorSessionNotice)
	})
}

// RebindExecutorIncarnation accepts an authenticated execution-owner observation for the same
// Sandbox UID. The new fence, handle retirement and model notice commit atomically before any
// command can use the replacement incarnation.
func (s *Store) RebindExecutorIncarnation(ctx context.Context, owner Owner, sandboxUID, previousUID, replacementUID, notDispatchedProcessID string) error {
	return s.rebindExecutorIncarnation(ctx, owner, sandboxUID, previousUID, replacementUID, notDispatchedProcessID, "", nil)
}

// RebindExecutorIncarnationWithSession atomically retires the old scope and binds an
// authenticated, newly opened empty execution session at the replacement incarnation.
func (s *Store) RebindExecutorIncarnationWithSession(ctx context.Context, owner Owner, sandboxUID, previousUID, replacementUID, sessionID string, shell *string) error {
	if sessionID == "" {
		return protocol.ErrInvalid
	}
	return s.rebindExecutorIncarnation(ctx, owner, sandboxUID, previousUID, replacementUID, "", sessionID, shell)
}

func (s *Store) rebindExecutorIncarnation(ctx context.Context, owner Owner, sandboxUID, previousUID, replacementUID, notDispatchedProcessID, replacementSessionID string, shell *string) error {
	if sandboxUID == "" || previousUID == "" || replacementUID == "" || previousUID == replacementUID {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		request := &samplerpb.CreateSessionRequest{}
		if err := proto.Unmarshal(session.Configuration, request); err != nil {
			return err
		}
		if request.GetExecutor().GetTarget().GetUid() != sandboxUID || request.GetExpectedIncarnationUid() != previousUID {
			return protocol.ErrPrecondition
		}
		request.ExpectedIncarnationUid = replacementUID
		wire, err := proto.Marshal(request)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_session SET configuration=$2,workspace_prepared=false WHERE id=$1`, session.ID, wire); err != nil {
			return err
		}
		session.Configuration = wire
		if err := s.recordEvidence(ctx, tx, owner, conversation, session, uuid.NewString(), eventpb.ExecutionProgress_KIND_EXECUTOR, eventpb.ExecutionProgress_PHASE_INCARNATION_CHANGED, map[string]any{
			"sandbox_uid": sandboxUID, "previous_incarnation_uid": previousUID, "replacement_incarnation_uid": replacementUID,
			"remote_outcome": NestedUnknown, "automatic_replay": false,
		}); err != nil {
			return err
		}
		notice := fmt.Sprintf("The execution Sandbox changed incarnation from %s to %s while retaining Sandbox UID %s (for example, after suspend and resume). A fresh execution session is being used. Old process and file handles cannot be reused; unfinished process outcomes remain unknown and no previous command was replayed. Persistent workspace files may remain, but verify any required process or temporary state before continuing.", previousUID, replacementUID, sandboxUID)
		if err := s.retireExecutorSession(ctx, tx, owner, conversation, session, notDispatchedProcessID, notice); err != nil {
			return err
		}
		if replacementSessionID != "" {
			conversation.ExecutorSession = &replacementSessionID
			conversation.ExecutorShell = shell
			return updateConversation(ctx, tx, conversation)
		}
		return nil
	})
}

func (s *Store) retireExecutorSession(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation, session *Session, notDispatchedProcessID, notice string) error {
	sessionID := ""
	if conversation.ExecutorSession != nil {
		sessionID = *conversation.ExecutorSession
	}
	rows, err := tx.QueryContext(ctx, `SELECT `+processColumns+` FROM sampler_process
		WHERE conversation_id=$1 AND status NOT IN ('exited','exited_unread','abandoned') FOR UPDATE`, conversation.ID)
	if err != nil {
		return err
	}
	var processes []*RemoteProcess
	for rows.Next() {
		process, err := scanProcess(rows)
		if err != nil {
			rows.Close()
			return err
		}
		processes = append(processes, process)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	if err := rows.Close(); err != nil {
		return err
	}
	for _, process := range processes {
		outcome := NestedUnknown
		if process.ID == notDispatchedProcessID && process.Status == protocol.ProcessStarting {
			outcome = NestedNotDispatched
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_process SET status='abandoned' WHERE id=$1 AND conversation_id=$2`, process.ID, conversation.ID); err != nil {
			return err
		}
		if err := s.recordEvidence(ctx, tx, owner, conversation, session, process.ID, eventpb.ExecutionProgress_KIND_PROCESS, eventpb.ExecutionProgress_PHASE_ABANDONED, map[string]any{
			"remote_outcome": outcome, "automatic_replay": false, "executor_session_id": sessionID,
			"previous_status": process.Status, "after_seq": process.AfterSequence, "exit_code": process.ExitCode,
			"buffered_bytes": len(process.BufferedOutput), "omitted_bytes": process.OmittedBytes,
		}); err != nil {
			return err
		}
	}
	conversation.ExecutorSession = nil
	conversation.ExecutorShell = nil
	// The session can be retired while the model's outer exec call is still pending.
	// Queue the notice against that call so it is appended after its tool result. This
	// preserves Anthropic's tool_result ordering while keeping the notice durable in the
	// same transaction as session retirement. With no pending call, append it directly.
	pendingCalls, err := conversation.Transcript.PendingCalls()
	if err != nil {
		return err
	}
	if len(pendingCalls) > 0 {
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_notification(conversation_id,call_id,cell_id,text) VALUES($1,$2,'',$3)`,
			conversation.ID, pendingCalls[0].CallID, notice); err != nil {
			return err
		}
	} else if err := conversation.apply(func(state protocol.Context) error {
		return state.AppendNotice([]protocol.ContentPart{{Type: "input_text", Text: notice}})
	}); err != nil {
		return err
	}
	return updateConversation(ctx, tx, conversation)
}

func (s *Store) checkRecovery(ctx context.Context, tx *sql.Tx, owner Owner) error {
	var id string
	err := tx.QueryRowContext(ctx, `SELECT id::text FROM sampler_intent WHERE conversation_id=$1 AND generation<>$2 AND result IS NULL LIMIT 1`, owner.ConversationID, owner.Generation).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	return &protocol.UnknownEffectError{Reason: "earlier owner left an unresolved effect"}
}

func (s *Store) CheckRecovery(ctx context.Context, owner Owner) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		return s.checkRecovery(ctx, tx, owner)
	})
}

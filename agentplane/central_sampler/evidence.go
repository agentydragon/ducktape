package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
)

const BoundaryBytes = 1024 * 1024

func (s *Store) ProtocolEvent(ctx context.Context, owner Owner, operationID string, kind eventpb.ExecutionProgress_Kind,
	phase eventpb.ExecutionProgress_Phase, payload map[string]any,
) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwnerHeader(ctx, tx, owner)
		if err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, operationID, kind, phase, payload)
	})
}

func sessionForConversation(ctx context.Context, tx *sql.Tx, conversationID string) (*Session, error) {
	session, err := scanSession(tx.QueryRowContext(ctx, `SELECT id,conversation_id,account_uid,configuration,model,effort,source_id,cursor,
		active_turn,active_command,workspace_prepared,setup_state,setup_started,blocked,blocked_reason FROM sampler_session WHERE conversation_id=$1 FOR UPDATE`, conversationID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, protocol.ErrMissing
	}
	return session, err
}

func (s *Store) recordEvidence(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation, session *Session,
	operationID string, kind eventpb.ExecutionProgress_Kind, phase eventpb.ExecutionProgress_Phase, payload any,
) error {
	_, err := s.recordEvidenceWithSources(ctx, tx, owner, conversation, session, operationID, kind, phase, payload)
	return err
}

func (s *Store) recordEvidenceWithSources(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation, session *Session,
	operationID string, kind eventpb.ExecutionProgress_Kind, phase eventpb.ExecutionProgress_Phase, payload any,
) ([]uint64, error) {
	parent := ""
	payloadMap, isMap := payload.(map[string]any)
	if cellID, ok := payloadMap["cell_id"]; isMap && ok && cellID != nil && fmt.Sprint(cellID) != "" {
		parent = fmt.Sprint(cellID)
	} else if conversation.Pending != nil && (kind == eventpb.ExecutionProgress_KIND_JAVASCRIPT || kind == eventpb.ExecutionProgress_KIND_EXECUTOR || kind == eventpb.ExecutionProgress_KIND_TOOL) {
		parent = conversation.Pending.ID
	} else if session.ActiveTurn != nil {
		parent = *session.ActiveTurn
	}

	encoded, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	count := (len(encoded) + BoundaryBytes - 1) / BoundaryBytes
	if count == 0 {
		count = 1
	}
	recordID := uuid.NewString()
	sources := make([]uint64, 0, count)
	for index := 0; index < count; index++ {
		start, end := index*BoundaryBytes, (index+1)*BoundaryBytes
		if end > len(encoded) {
			end = len(encoded)
		}
		entry, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_BoundaryRecord{
			BoundaryRecord: &eventpb.BoundaryRecord{
				Boundary: boundaryName(kind.String(), "KIND_"), Direction: boundaryName(phase.String(), "PHASE_"), Payload: append([]byte(nil), encoded[start:end]...),
				MediaType: "application/json", CorrelationId: operationID, ParentId: parent,
				RecordId: recordID, FragmentIndex: uint32(index), FragmentCount: uint32(count),
			},
		}})
		if err != nil {
			return nil, err
		}
		sources = append(sources, entry.GetOrigin().GetSequence())
	}
	if _, err := appendEvent(ctx, tx, session, &eventpb.Event{
		SourceSequences: sources,
		Observation: &eventpb.Event_ExecutionProgress{ExecutionProgress: &eventpb.ExecutionProgress{
			OperationId: operationID, ParentId: parent, Kind: kind, Phase: phase,
		}},
	}); err != nil {
		return nil, err
	}

	if kind == eventpb.ExecutionProgress_KIND_CELL && phase == eventpb.ExecutionProgress_PHASE_RESULT && isMap && payloadMap["call_id"] != nil && payloadMap["output"] != nil {
		callID, _ := payloadMap["call_id"].(string)
		succeeded := true
		if success, ok := payloadMap["success"].(bool); ok {
			succeeded = success
		}
		output, err := json.Marshal(payloadMap["output"])
		if err != nil {
			return nil, err
		}
		_, err = appendEvent(ctx, tx, session, &eventpb.Event{
			SourceSequences: sources,
			Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
				ItemId: callID, Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: protocol.DisplayText(string(output)), Succeeded: succeeded}},
			}},
		})
		if err != nil {
			return nil, err
		}
	}
	return sources, nil
}

// boundaryName is the BoundaryRecord spelling of a progress kind or phase: its lowercased value name.
func boundaryName(valueName, prefix string) string {
	return strings.ToLower(strings.TrimPrefix(valueName, prefix))
}

func (kind IntentKind) progressKind() eventpb.ExecutionProgress_Kind {
	switch kind {
	case IntentCell:
		return eventpb.ExecutionProgress_KIND_CELL
	case IntentSampling:
		return eventpb.ExecutionProgress_KIND_SAMPLING
	case IntentCompaction:
		return eventpb.ExecutionProgress_KIND_COMPACTION
	case IntentInterrupt:
		return eventpb.ExecutionProgress_KIND_INTERRUPT
	case IntentTool:
		return eventpb.ExecutionProgress_KIND_TOOL
	case IntentExecutor:
		return eventpb.ExecutionProgress_KIND_EXECUTOR
	case IntentInterruptProcess:
		return eventpb.ExecutionProgress_KIND_INTERRUPT_PROCESS
	default:
		panic(fmt.Sprintf("unvalidated intent kind %q", string(kind)))
	}
}

func processPhase(status protocol.ProcessStatus) eventpb.ExecutionProgress_Phase {
	switch status {
	case protocol.ProcessStarting:
		return eventpb.ExecutionProgress_PHASE_STARTING
	case protocol.ProcessRunning:
		return eventpb.ExecutionProgress_PHASE_RUNNING
	case protocol.ProcessExited:
		return eventpb.ExecutionProgress_PHASE_EXITED
	case protocol.ProcessExitedUnread:
		return eventpb.ExecutionProgress_PHASE_EXITED_UNREAD
	case protocol.ProcessAbandoned:
		return eventpb.ExecutionProgress_PHASE_ABANDONED
	case protocol.ProcessUnknown:
		return eventpb.ExecutionProgress_PHASE_UNKNOWN
	default:
		panic(fmt.Sprintf("unvalidated process status %q", string(status)))
	}
}

func cellPhase(status protocol.CellStatus) eventpb.ExecutionProgress_Phase {
	switch status {
	case protocol.CellLive:
		return eventpb.ExecutionProgress_PHASE_LIVE
	case protocol.CellCompleted:
		return eventpb.ExecutionProgress_PHASE_COMPLETED
	case protocol.CellTerminated:
		return eventpb.ExecutionProgress_PHASE_TERMINATED
	case protocol.CellAbandoned:
		return eventpb.ExecutionProgress_PHASE_ABANDONED
	default:
		panic(fmt.Sprintf("unvalidated cell status %q", string(status)))
	}
}

// nativeFramePhase maps an executor native frame's direction, as the execution adapter writes it.
func nativeFramePhase(direction string) (eventpb.ExecutionProgress_Phase, error) {
	switch direction {
	case "request":
		return eventpb.ExecutionProgress_PHASE_REQUEST, nil
	case "response":
		return eventpb.ExecutionProgress_PHASE_RESPONSE, nil
	case "notification":
		return eventpb.ExecutionProgress_PHASE_NOTIFICATION, nil
	default:
		return eventpb.ExecutionProgress_PHASE_UNSPECIFIED, fmt.Errorf("executor native frame has unknown direction=%q", direction)
	}
}

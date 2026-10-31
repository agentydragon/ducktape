package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/google/uuid"
)

const BoundaryBytes = 1024 * 1024

func (s *Store) ProtocolEvent(ctx context.Context, owner Owner, operationID, kind, phase string, payload map[string]any) error {
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
	operationID, kind, phase string, payload any,
) error {
	_, err := s.recordEvidenceWithSources(ctx, tx, owner, conversation, session, operationID, kind, phase, payload)
	return err
}

func (s *Store) recordEvidenceWithSources(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation, session *Session,
	operationID, kind, phase string, payload any,
) ([]uint64, error) {
	parent := ""
	payloadMap, isMap := payload.(map[string]any)
	if cellID, ok := payloadMap["cell_id"]; isMap && ok && cellID != nil && fmt.Sprint(cellID) != "" {
		parent = fmt.Sprint(cellID)
	} else if conversation.Pending != nil && protocol.IsOneOf(kind, "javascript", "executor", "tool") {
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
				Boundary: kind, Direction: phase, Payload: append([]byte(nil), encoded[start:end]...),
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
			OperationId: operationID, ParentId: parent, Kind: kind, Phase: phase, PayloadJson: "{}",
		}},
	}); err != nil {
		return nil, err
	}

	if kind == "cell" && phase == "result" && isMap && payloadMap["call_id"] != nil && payloadMap["output"] != nil {
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

func stringValue(value any) string {
	text, _ := value.(string)
	return text
}

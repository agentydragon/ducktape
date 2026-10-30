package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"unicode/utf8"

	"github.com/google/uuid"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

const (
	DisplayBytes  = 256 * 1024
	BoundaryBytes = 1024 * 1024
)

func DisplayText(value string) string {
	encoded := []byte(value)
	if len(encoded) <= DisplayBytes {
		return value
	}
	cut := encoded[:DisplayBytes]
	for len(cut) > 0 && !utf8.Valid(cut) {
		cut = cut[:len(cut)-1]
	}
	return string(cut) + "\n[Display truncated; full content is retained in source boundary records.]"
}

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

func (s *Store) ProviderEvent(ctx context.Context, owner Owner, intentID string, event ResponsesEvent) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwnerHeader(ctx, tx, owner)
		if err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		return s.recordEvidence(ctx, tx, owner, conversation, session, intentID, "provider", "received", event)
	})
}

func sessionForConversation(ctx context.Context, tx *sql.Tx, conversationID string) (*Session, error) {
	session, err := scanSession(tx.QueryRowContext(ctx, `SELECT id,conversation_id,account_uid,configuration,model,effort,source_id,cursor,
		active_turn,active_command,workspace_prepared,setup_state,setup_started,blocked,blocked_reason FROM sampler_session WHERE conversation_id=$1 FOR UPDATE`, conversationID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrMissing
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
	} else if conversation.Pending != nil && isOneOf(kind, "javascript", "executor", "tool") {
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
	if kind == "provider" {
		event, ok := payload.(ResponsesEvent)
		if !ok {
			return nil, ErrInvalid
		}
		if err := s.providerObservation(ctx, tx, session, sources, event); err != nil {
			return nil, err
		}
	}
	if kind == "cell" && phase == "result" && isMap && (payloadMap["type"] == string(ResponsesItemCustomToolOutput) || payloadMap["type"] == string(ResponsesItemFunctionOutput)) {
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
				ItemId: callID, Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: DisplayText(string(output)), Succeeded: succeeded}},
			}},
		})
		if err != nil {
			return nil, err
		}
	}
	return sources, nil
}

func (s *Store) providerObservation(ctx context.Context, tx *sql.Tx, session *Session, sources []uint64, event ResponsesEvent) error {
	switch event.Type {
	case "response.output_text.delta", "response.reasoning_summary_text.delta", "response.reasoning_text.delta":
		if event.Delta == nil {
			return ErrInvalid
		}
		_, err := appendEvent(ctx, tx, session, &eventpb.Event{
			SourceSequences: sources,
			Observation:     &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{ItemId: event.ItemID, Text: DisplayText(*event.Delta)}},
		})
		return err
	case "response.output_item.added", "response.output_item.done":
		if event.Item == nil {
			return ErrInvalid
		}
		item := *event.Item
		kinds := map[string]eventpb.ItemKind{
			string(ResponsesItemMessage):        eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT,
			string(ResponsesItemReasoning):      eventpb.ItemKind_ITEM_KIND_REASONING,
			string(ResponsesItemCustomToolCall): eventpb.ItemKind_ITEM_KIND_TOOL_CALL,
			string(ResponsesItemFunctionCall):   eventpb.ItemKind_ITEM_KIND_TOOL_CALL,
		}
		kind, supported := kinds[string(item.Type)]
		if !supported {
			return nil
		}
		id := item.CallID
		if id == "" {
			id = item.ID
		}
		if event.Type == ResponsesEventOutputItemAdded {
			_, err := appendEvent(ctx, tx, session, &eventpb.Event{
				SourceSequences: sources,
				Observation:     &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: id, Kind: kind, ToolName: item.Name}},
			})
			return err
		}
		if call, ok := item.ToolCall(); ok {
			// Function arguments are already JSON; raw tool input is wrapped as its source.
			arguments := []byte(call.Input)
			if call.Type != ResponsesItemFunctionCall || len(arguments) > DisplayBytes || !json.Valid(arguments) {
				var err error
				arguments, err = json.Marshal(struct {
					Source string `json:"source"`
				}{Source: DisplayText(call.Input)})
				if err != nil {
					return err
				}
			}
			_, err := appendEvent(ctx, tx, session, &eventpb.Event{
				SourceSequences: sources,
				Observation:     &eventpb.Event_ToolArguments{ToolArguments: &eventpb.ToolArguments{ItemId: id, ArgumentsJson: string(arguments)}},
			})
			return err
		}
		text, err := responsesText(item)
		if err != nil {
			return err
		}
		_, err = appendEvent(ctx, tx, session, &eventpb.Event{
			SourceSequences: sources,
			Observation:     &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{ItemId: id, Outcome: &eventpb.ItemCompleted_Text{Text: DisplayText(text)}}},
		})
		return err
	default:
		return nil
	}
}

func stringValue(value any) string {
	text, _ := value.(string)
	return text
}

func isOneOf(value string, options ...string) bool {
	for _, option := range options {
		if value == option {
			return true
		}
	}
	return false
}

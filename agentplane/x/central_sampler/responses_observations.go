package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

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
		sources, err := s.recordEvidenceWithSources(ctx, tx, owner, conversation, session, intentID, "provider", "received", event)
		if err != nil {
			return err
		}
		return s.providerObservation(ctx, tx, session, sources, event)
	})
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
			if call.Encoding != ExecEncodingJSON || len(arguments) > DisplayBytes || !json.Valid(arguments) {
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

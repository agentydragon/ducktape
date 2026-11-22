package responses

import (
	"encoding/json"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

// observationProjector correlates provider item IDs with the call IDs used by the journal.
type observationProjector struct {
	calls map[string]string
}

// observations never changes or replaces the provider's raw event record.
func (p *observationProjector) observations(event Event) ([]*eventpb.Event, error) {
	switch event.Type {
	case EventFunctionCallArgumentsDelta, EventCustomToolCallInputDelta:
		if event.Delta == nil {
			return nil, protocol.ErrInvalid
		}
		id, ok := p.calls[event.ItemID]
		if !ok || *event.Delta == "" {
			return nil, nil
		}
		return []*eventpb.Event{{Observation: &eventpb.Event_ToolArgumentsDelta{ToolArgumentsDelta: &eventpb.ToolArgumentsDelta{ItemId: id, PartialJson: protocol.DisplayText(*event.Delta)}}}}, nil
	case "response.output_text.delta", "response.reasoning_summary_text.delta", "response.reasoning_text.delta":
		if event.Delta == nil {
			return nil, protocol.ErrInvalid
		}
		return []*eventpb.Event{{Observation: &eventpb.Event_TextDelta{
			TextDelta: &eventpb.TextDelta{ItemId: event.ItemID, Text: protocol.DisplayText(*event.Delta)},
		}}}, nil
	case EventOutputItemAdded, EventOutputItemDone:
		if event.Item == nil {
			return nil, protocol.ErrInvalid
		}
		item := *event.Item
		kinds := map[ItemType]eventpb.ItemKind{
			ItemMessage:        eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT,
			ItemReasoning:      eventpb.ItemKind_ITEM_KIND_REASONING,
			ItemCustomToolCall: eventpb.ItemKind_ITEM_KIND_TOOL_CALL,
			ItemFunctionCall:   eventpb.ItemKind_ITEM_KIND_TOOL_CALL,
		}
		kind, supported := kinds[item.Type]
		if !supported {
			return nil, nil
		}
		id := item.CallID
		if id == "" {
			id = item.ID
		}
		if event.Type == EventOutputItemAdded {
			if (item.Type == ItemCustomToolCall || item.Type == ItemFunctionCall) && item.ID != "" && id != "" {
				if p.calls == nil {
					p.calls = make(map[string]string)
				}
				p.calls[item.ID] = id
			}
			return []*eventpb.Event{{Observation: &eventpb.Event_ItemStarted{
				ItemStarted: &eventpb.ItemStarted{ItemId: id, Kind: kind, ToolName: item.Name},
			}}}, nil
		}
		if call, ok := item.ToolCall(); ok {
			arguments := []byte(call.Input)
			if call.Encoding != protocol.ExecEncodingJSON || len(arguments) > protocol.DisplayBytes || !json.Valid(arguments) {
				var err error
				arguments, err = json.Marshal(struct {
					Source string `json:"source"`
				}{Source: protocol.DisplayText(call.Input)})
				if err != nil {
					return nil, err
				}
			}
			return []*eventpb.Event{{Observation: &eventpb.Event_ToolArguments{
				ToolArguments: &eventpb.ToolArguments{ItemId: id, ArgumentsJson: string(arguments)},
			}}}, nil
		}
		text, err := Text(item)
		if err != nil {
			return nil, err
		}
		return []*eventpb.Event{{Observation: &eventpb.Event_ItemCompleted{
			ItemCompleted: &eventpb.ItemCompleted{ItemId: id, Outcome: &eventpb.ItemCompleted_Text{Text: protocol.DisplayText(text)}},
		}}}, nil
	default:
		return nil, nil
	}
}

package anthropic

import (
	"encoding/json"
	"fmt"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

func anthropicHistoryItemID(message map[string]json.RawMessage, index int, block map[string]json.RawMessage) string {
	if rawString(block, "type") == "tool_use" {
		return rawString(block, "id")
	}
	messageID := rawString(message, "id")
	if messageID == "" {
		return ""
	}
	return fmt.Sprintf("%s#%d", messageID, index)
}

func anthropicItemStarted(message map[string]json.RawMessage, index int, block map[string]json.RawMessage) *eventpb.Event {
	itemID := anthropicHistoryItemID(message, index, block)
	if itemID == "" {
		return nil
	}
	var kind eventpb.ItemKind
	var toolName string
	switch rawString(block, "type") {
	case "text":
		kind = eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT
	case "thinking":
		kind = eventpb.ItemKind_ITEM_KIND_REASONING
	case "tool_use":
		kind, toolName = eventpb.ItemKind_ITEM_KIND_TOOL_CALL, rawString(block, "name")
	default:
		return nil
	}
	return &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
		ItemId: itemID, Kind: kind, ToolName: toolName,
	}}}
}

func anthropicBlockDelta(message map[string]json.RawMessage, index int, block map[string]json.RawMessage, deltaType, delta string) *eventpb.Event {
	itemID := anthropicHistoryItemID(message, index, block)
	if itemID == "" || delta == "" {
		return nil
	}
	switch deltaType {
	case "text_delta":
		return &eventpb.Event{Observation: &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{
			ItemId: itemID, Text: protocol.DisplayText(delta),
		}}}
	case "thinking_delta":
		return &eventpb.Event{Observation: &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{
			ItemId: itemID, Text: protocol.DisplayText(delta),
		}}}
	case "input_json_delta":
		return &eventpb.Event{Observation: &eventpb.Event_ToolArgumentsDelta{ToolArgumentsDelta: &eventpb.ToolArgumentsDelta{
			ItemId: itemID, PartialJson: protocol.DisplayText(delta),
		}}}
	default:
		return nil
	}
}

func anthropicBlockCompleted(message map[string]json.RawMessage, index int, block map[string]json.RawMessage) *eventpb.Event {
	itemID := anthropicHistoryItemID(message, index, block)
	if itemID == "" {
		return nil
	}
	var text string
	switch rawString(block, "type") {
	case "text":
		text = rawString(block, "text")
	case "thinking":
		text = rawString(block, "thinking")
	default:
		return nil
	}
	return &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
		ItemId: itemID, Outcome: &eventpb.ItemCompleted_Text{Text: protocol.DisplayText(text)},
	}}}
}

func anthropicToolArguments(message map[string]json.RawMessage, index int, block map[string]json.RawMessage) *eventpb.Event {
	itemID := anthropicHistoryItemID(message, index, block)
	arguments := block["input"]
	if itemID == "" || !json.Valid(arguments) {
		return nil
	}
	encoded := string(arguments)
	if len(arguments) > protocol.DisplayBytes {
		source, err := json.Marshal(struct {
			Source string `json:"source"`
		}{Source: protocol.DisplayText(encoded)})
		if err != nil {
			return nil
		}
		encoded = string(source)
	}
	return &eventpb.Event{Observation: &eventpb.Event_ToolArguments{ToolArguments: &eventpb.ToolArguments{
		ItemId: itemID, ArgumentsJson: encoded,
	}}}
}

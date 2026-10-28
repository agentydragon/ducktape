package journal

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/runner/harness"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

// ObservedItems reconstructs durable item evidence for a turn. Earlier events are replayed
// only for items that appear in the selected turn, which includes tools that finish later.
func (j *Journal) ObservedItems(ctx context.Context, turnID string) (map[string]harness.ObservedItem, error) {
	turnEntries, err := j.turnEvents(ctx, turnID, false)
	if err != nil {
		return nil, err
	}
	itemIDs := map[string]struct{}{}
	for _, entry := range turnEntries {
		if itemID := eventItemID(entry.GetEvent()); itemID != "" {
			itemIDs[itemID] = struct{}{}
		}
	}
	items := make(map[string]harness.ObservedItem, len(itemIDs))
	entries, err := j.turnEvents(ctx, turnID, true)
	if err != nil {
		return nil, err
	}
	ownerTurn := ""
	for _, entry := range entries {
		event := entry.GetEvent()
		if started := event.GetTurnStarted(); started != nil {
			ownerTurn = started.GetTurnId()
		}
		itemID := eventItemID(event)
		if _, belongs := itemIDs[itemID]; !belongs {
			continue
		}
		if event.GetItemStarted() == nil {
			if _, started := items[itemID]; !started {
				return nil, fmt.Errorf("cannot recover turn %q: item %q at cursor %d has no preceding start", turnID, itemID, entry.GetCursor())
			}
		}
		switch {
		case event.GetItemStarted() != nil:
			started := event.GetItemStarted()
			items[started.GetItemId()] = harness.ObservedItem{
				TurnID: ownerTurn, ItemID: started.GetItemId(), Kind: started.GetKind(), ToolName: started.GetToolName(), FirstCursor: entry.GetCursor(),
			}
		case event.GetTextDelta() != nil:
			delta := event.GetTextDelta()
			item := items[delta.GetItemId()]
			item.Text += delta.GetText()
			items[item.ItemID] = item
		case event.GetToolArgumentsDelta() != nil:
			delta := event.GetToolArgumentsDelta()
			item := items[delta.GetItemId()]
			item.Arguments += delta.GetPartialJson()
			items[item.ItemID] = item
		case event.GetToolArguments() != nil:
			arguments := event.GetToolArguments()
			item := items[arguments.GetItemId()]
			item.Arguments = arguments.GetArgumentsJson()
			items[item.ItemID] = item
		case event.GetToolOutputDelta() != nil:
			delta := event.GetToolOutputDelta()
			item := items[delta.GetItemId()]
			item.Output += delta.GetText()
			items[item.ItemID] = item
		case event.GetItemCompleted() != nil:
			completed := event.GetItemCompleted()
			item := items[completed.GetItemId()]
			item.Completed = true
			outcome := completed.ProtoReflect().WhichOneof(completed.ProtoReflect().Descriptor().Oneofs().ByName("outcome"))
			if outcome != nil && outcome.Name() == "text" {
				item.Text = completed.GetText()
			} else if tool := completed.GetTool(); tool != nil {
				item.Output = tool.GetOutput()
			}
			items[item.ItemID] = item
		}
	}
	return items, nil
}

func eventItemID(event *eventpb.Event) string {
	if event == nil {
		return ""
	}
	switch {
	case event.GetItemStarted() != nil:
		return event.GetItemStarted().GetItemId()
	case event.GetTextDelta() != nil:
		return event.GetTextDelta().GetItemId()
	case event.GetToolArgumentsDelta() != nil:
		return event.GetToolArgumentsDelta().GetItemId()
	case event.GetToolArguments() != nil:
		return event.GetToolArguments().GetItemId()
	case event.GetToolOutputDelta() != nil:
		return event.GetToolOutputDelta().GetItemId()
	case event.GetItemCompleted() != nil:
		return event.GetItemCompleted().GetItemId()
	default:
		return ""
	}
}

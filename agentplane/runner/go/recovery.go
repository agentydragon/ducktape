package runner

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"sort"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

// ObservedItem is the runner's durable view of an item emitted during a turn.
type ObservedItem struct {
	ItemID      string
	Kind        eventpb.ItemKind
	ToolName    string
	Text        string
	Arguments   string
	Output      string
	Completed   bool
	firstCursor uint64
}

// ObservedItems reconstructs durable item evidence for a turn. Earlier events are replayed
// only for items that appear in the selected turn, which includes tools that finish later.
func (j *Journal) ObservedItems(ctx context.Context, turnID string) (map[string]ObservedItem, error) {
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
	items := make(map[string]ObservedItem, len(itemIDs))
	entries, err := j.turnEvents(ctx, turnID, true)
	if err != nil {
		return nil, err
	}
	for _, entry := range entries {
		event := entry.GetEvent()
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
			items[started.GetItemId()] = ObservedItem{
				ItemID: started.GetItemId(), Kind: started.GetKind(), ToolName: started.GetToolName(), firstCursor: entry.GetCursor(),
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

func CompareItem(observed ObservedItem, recovered *ObservedItem) *eventpb.ItemRecovery {
	if recovered == nil {
		return &eventpb.ItemRecovery{
			ItemId: observed.ItemID, Disposition: eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT,
		}
	}
	if observed.Kind == recovered.Kind && observed.Text == recovered.Text &&
		argumentsEqual(observed.Arguments, recovered.Arguments) && observed.Output == recovered.Output {
		return &eventpb.ItemRecovery{
			ItemId: observed.ItemID, Disposition: eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_RETAINED,
		}
	}
	return &eventpb.ItemRecovery{
		ItemId:      observed.ItemID,
		Disposition: eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED,
		Replacement: &eventpb.RecoveredContent{
			Text: recovered.Text, ArgumentsJson: recovered.Arguments, Output: recovered.Output,
		},
	}
}

func argumentsEqual(left, right string) bool {
	if left == right {
		return true
	}
	var leftValue, rightValue any
	if err := json.Unmarshal([]byte(left), &leftValue); err != nil {
		return false
	}
	if err := json.Unmarshal([]byte(right), &rightValue); err != nil {
		return false
	}
	return reflect.DeepEqual(leftValue, rightValue)
}

func UnknownItem(itemID, reason string) *eventpb.ItemRecovery {
	return &eventpb.ItemRecovery{
		ItemId: itemID, Disposition: eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN, Reason: reason,
	}
}

func UnknownReport(turnID string, items map[string]ObservedItem, reason string) *eventpb.ConversationReconciled {
	itemIDs := make([]string, 0, len(items))
	for itemID := range items {
		itemIDs = append(itemIDs, itemID)
	}
	sort.Slice(itemIDs, func(i, j int) bool {
		left, right := items[itemIDs[i]].firstCursor, items[itemIDs[j]].firstCursor
		if left != right {
			return left < right
		}
		return itemIDs[i] < itemIDs[j]
	})
	report := &eventpb.ConversationReconciled{TurnId: turnID, Items: make([]*eventpb.ItemRecovery, 0, len(itemIDs))}
	for _, itemID := range itemIDs {
		report.Items = append(report.Items, UnknownItem(itemID, reason))
	}
	return report
}

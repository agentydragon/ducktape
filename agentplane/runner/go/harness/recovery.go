package harness

import (
	"encoding/json"
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
	FirstCursor uint64
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
		left, right := items[itemIDs[i]].FirstCursor, items[itemIDs[j]].FirstCursor
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

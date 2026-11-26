package harness

import (
	"context"
	"encoding/json"
	"io"
	"reflect"
	"sort"
	"strings"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

// ReplayNativeFrames visits both directions from the earliest relevant command admission.
// A handler may append events. Read through the current tail, advancing past non-native entries.
func ReplayNativeFrames(ctx context.Context, journal AdapterJournal, firstAdmission uint64, visit func(*eventpb.Native, uint64) error) error {
	cursor := firstAdmission
	if cursor > 0 {
		cursor-- // Since reads strictly after the supplied cursor.
	}
	for cursor < journal.LastCursor() {
		entries, err := journal.Since(ctx, cursor, 128)
		if err != nil {
			return err
		}
		if len(entries) == 0 {
			break
		}
		for _, entry := range entries {
			cursor = entry.GetCursor()
			if native := entry.GetEvent().GetNative(); native != nil {
				if err := visit(native, entry.GetOrigin().GetSequence()); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// ObservedItem is the Runtime's durable view of an item emitted during a turn.
type ObservedItem struct {
	// TurnID owns the item even when its completion appears during a later turn.
	TurnID      string
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
	// Ignore object order and whitespace, but keep numeric tokens exact. In
	// particular, decoding through float64 could hide a changed tool argument.
	values := make([]any, 2)
	for i, raw := range []string{left, right} {
		decoder := json.NewDecoder(strings.NewReader(raw))
		decoder.UseNumber()
		if err := decoder.Decode(&values[i]); err != nil {
			return false
		}
		if err := decoder.Decode(new(any)); err != io.EOF {
			return false
		}
	}
	return reflect.DeepEqual(values[0], values[1])
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

package journal

import (
	"context"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

func TestObservedItemKeepsOwningTurnWhenItCompletesLater(t *testing.T) {
	journal := openTestJournal(t)
	ctx := context.Background()
	for _, event := range []*eventpb.Event{
		{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "owner"}}},
		{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: "tool", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "commandExecution"}}},
		{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: "owner", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}}},
		{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "later"}}},
		{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{ItemId: "tool", Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: "done", Succeeded: true}}}}},
		{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: "later", Status: eventpb.TurnStatus_TURN_STATUS_INTERRUPTED}}},
	} {
		if _, err := journal.Append(ctx, event, AppendOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	items, err := journal.ObservedItems(ctx, "later")
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items["tool"].TurnID != "owner" || !items["tool"].Completed || items["tool"].Output != "done" {
		t.Fatalf("lost item owner or later completion: %+v", items)
	}
}

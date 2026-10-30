package codex

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"strings"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/runner/codex/protocol"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/jsonrpc"
)

// Codex history is a display projection. The paginated contract preserves the tested visible
// items; legacy history loses identities and calls. Never fall back to private Codex storage.
// See agentplane/debug/codex_app_server_history_apis.md for the real-binary characterization.
type codexHistorySnapshot struct {
	items map[string]protocol.ThreadItemEntry
	err   error
}

func (a *codexAdapter) Reconcile(ctx context.Context, turnID string, resumed bool) (*eventpb.ConversationReconciled, error) {
	a.historyMu.Lock()
	defer a.historyMu.Unlock()
	observed, err := a.host.Journal().ObservedItems(ctx, turnID)
	if err != nil {
		return nil, err
	}
	if len(observed) == 0 {
		return &eventpb.ConversationReconciled{TurnId: turnID}, nil
	}
	a.mu.Lock()
	mode := a.historyMode
	a.mu.Unlock()
	if mode == nil || *mode != protocol.Paginated {
		return harness.UnknownReport(turnID, observed, "Codex continuation requires paginated history"), nil
	}
	snapshot := a.resumedHistory
	if !resumed || snapshot == nil {
		turns := []string{turnID}
		if resumed {
			turns, err = a.host.Journal().RecoveryTurns(ctx)
			if err != nil {
				return nil, err
			}
		}
		ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		snapshot = a.readHistory(ctx, turns)
		if resumed {
			a.resumedHistory = snapshot
		}
	}
	if snapshot.err != nil {
		return harness.UnknownReport(turnID, observed, "cannot establish Codex continuation: "+snapshot.err.Error()), nil
	}
	report := &eventpb.ConversationReconciled{TurnId: turnID}
	for _, id := range slices.Sorted(maps.Keys(observed)) {
		item := observed[id]
		if item.Kind != eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT && item.Kind != eventpb.ItemKind_ITEM_KIND_REASONING &&
			(item.Kind != eventpb.ItemKind_ITEM_KIND_TOOL_CALL || item.ToolName != "commandExecution") {
			report.Items = append(report.Items, harness.UnknownItem(id, "unsupported Codex continuation item: "+item.ToolName))
			continue
		}
		saved, ok := snapshot.items[id]
		if !ok {
			// A command may have run without a durable model-facing result. A missing display
			// item cannot prove that its synthetic interruption output is absent from context.
			if item.Kind == eventpb.ItemKind_ITEM_KIND_TOOL_CALL {
				report.Items = append(report.Items, harness.UnknownItem(id, "Codex history does not establish the tool continuation"))
			} else {
				report.Items = append(report.Items, harness.CompareItem(item, nil))
			}
			continue
		}
		recovered := item
		valid := true
		switch {
		case item.Kind == eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT && saved.Item.Type == protocol.ThreadItemTypeAgentMessage && saved.Item.Text != nil:
			recovered.Text = *saved.Item.Text
		case item.Kind == eventpb.ItemKind_ITEM_KIND_REASONING && saved.Item.Type == protocol.ThreadItemTypeReasoning:
			recovered.Text = strings.Join(saved.Item.Summary, "\n")
		case item.Kind == eventpb.ItemKind_ITEM_KIND_TOOL_CALL && saved.Item.Type == protocol.ThreadItemTypeCommandExecution:
			native := saved.Item
			if !item.Completed || native.Status == nil || *native.Status != "completed" ||
				native.AggregatedOutput == nil || native.Command == nil || native.Cwd == nil {
				valid = false
				break
			}
			arguments, err := json.Marshal(map[string]string{"command": *native.Command, "cwd": *native.Cwd})
			if err != nil {
				return nil, err
			}
			recovered.Arguments = string(arguments)
			recovered.Output = *native.AggregatedOutput
		default:
			valid = false
		}
		if !valid {
			report.Items = append(report.Items, harness.UnknownItem(id, "Codex history does not establish this item's model continuation"))
		} else {
			report.Items = append(report.Items, harness.CompareItem(item, &recovered))
		}
	}
	return report, nil
}

// Read only turns selected by the runner's recovery window, plus owners of items that finish in a
// later turn. One snapshot serves all turns in a resume, including any compaction barrier in that
// window. Later turns with no observed items still need a compaction check when validating earlier
// items. The stdout reader remains free to deliver these replies.
func (a *codexAdapter) readHistory(ctx context.Context, turns []string) *codexHistorySnapshot {
	snapshot := &codexHistorySnapshot{items: make(map[string]protocol.ThreadItemEntry)}
	owners := make(map[string]bool)
	hasObserved := false
	for _, turn := range turns {
		observed, err := a.host.Journal().ObservedItems(ctx, turn)
		if err != nil {
			snapshot.err = err
			return snapshot
		}
		owners[turn] = true
		if len(observed) != 0 {
			hasObserved = true
		}
		for _, item := range observed {
			if item.TurnID != "" {
				owners[item.TurnID] = true
			}
		}
	}
	if !hasObserved {
		return snapshot
	}
	for _, turn := range slices.Sorted(maps.Keys(owners)) {
		if err := a.readTurnHistory(ctx, turn, snapshot.items); err != nil {
			snapshot.err = err
			return snapshot
		}
	}
	return snapshot
}

func (a *codexAdapter) readTurnHistory(ctx context.Context, turnID string, items map[string]protocol.ThreadItemEntry) error {
	limit := int64(100) // The pinned server's maximum: bound each response without needless round trips.
	a.mu.Lock()
	threadID := a.threadID
	a.mu.Unlock()
	params := protocol.ThreadItemsListParams{ThreadID: threadID, TurnID: &turnID, Limit: &limit}
	cursors := make(map[string]bool)
	for {
		requestID, err := a.nextID()
		if err != nil {
			return err
		}
		frame, err := a.request(ctx, jsonrpc.Request[any]{ID: requestID, Method: "thread/items/list", Params: params})
		if err != nil {
			return err
		}
		if err := codexResponseError(frame); err != nil {
			return err
		}
		var page protocol.ThreadItemsListResponse
		if err := json.Unmarshal(frame.Result, &page); err != nil {
			return err
		}
		if page.Data == nil {
			return fmt.Errorf("missing Codex history page data")
		}
		for _, entry := range page.Data {
			if entry.TurnID != turnID || entry.Item.ID == "" {
				return fmt.Errorf("invalid Codex history item scope or identity")
			}
			if entry.Item.Type == protocol.ContextCompaction {
				return fmt.Errorf("Codex compacted the recovery window; display history does not establish model continuation")
			}
			if _, exists := items[entry.Item.ID]; exists {
				return fmt.Errorf("duplicate Codex history item %q", entry.Item.ID)
			}
			items[entry.Item.ID] = entry
		}
		if page.NextCursor == nil {
			return nil
		}
		if *page.NextCursor == "" || cursors[*page.NextCursor] {
			return fmt.Errorf("invalid or repeated Codex history cursor")
		}
		cursors[*page.NextCursor] = true
		params.Cursor = &protocol.ThreadItemsListCursor{String: page.NextCursor}
	}
}

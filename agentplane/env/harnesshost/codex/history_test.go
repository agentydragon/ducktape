package codex

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/codex/protocol"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/harness"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

type historyJournal struct {
	harness.AdapterJournal
	observed map[string]map[string]harness.ObservedItem
	turns    []string
}

func (j *historyJournal) ObservedItems(_ context.Context, turn string) (map[string]harness.ObservedItem, error) {
	return j.observed[turn], nil
}
func (j *historyJournal) RecoveryTurns(context.Context) ([]string, error) { return j.turns, nil }

type historyHost struct {
	harness.AdapterHost
	journal  *historyJournal
	pages    []string
	requests []protocol.ThreadItemsListParams
}

func (h *historyHost) Journal() harness.AdapterJournal { return h.journal }
func (h *historyHost) RequestNativeHandshake(_ context.Context, raw json.RawMessage, matches func(json.RawMessage) bool) (harness.NativeReceipt, error) {
	var request struct {
		ID     string                         `json:"id"`
		Method string                         `json:"method"`
		Params protocol.ThreadItemsListParams `json:"params"`
	}
	if err := json.Unmarshal(raw, &request); err != nil {
		return harness.NativeReceipt{}, err
	}
	if request.Method != "thread/items/list" {
		return harness.NativeReceipt{}, fmt.Errorf("unexpected RPC: %s", request.Method)
	}
	h.requests = append(h.requests, request.Params)
	if len(h.pages) == 0 {
		return harness.NativeReceipt{}, errors.New("unexpected extra history RPC")
	}
	page := h.pages[0]
	h.pages = h.pages[1:]
	raw = json.RawMessage(fmt.Sprintf(`{"id":%q,%s}`, request.ID, page))
	if !matches(raw) {
		return harness.NativeReceipt{}, errors.New("history RPC response did not match")
	}
	return harness.NativeReceipt{Frame: raw}, nil
}

func newHistoryAdapter(items map[string]harness.ObservedItem, pages ...string) (*codexAdapter, *historyHost) {
	mode := protocol.Paginated
	host := &historyHost{journal: &historyJournal{observed: map[string]map[string]harness.ObservedItem{"turn": items}, turns: []string{"turn"}}, pages: pages}
	return &codexAdapter{host: host, threadID: "thread", historyMode: &mode}, host
}

func assertDisposition(t *testing.T, report *eventpb.ConversationReconciled, id string, want eventpb.RecoveryDisposition) {
	t.Helper()
	for _, item := range report.Items {
		if item.ItemId == id {
			if item.Disposition != want {
				t.Fatalf("%s: %v, want %s", id, item, want)
			}
			return
		}
	}
	t.Fatalf("no recovery for %s: %v", id, report)
}

func TestHistoryReadsOnlyRequestedTurnAndFollowsEveryPage(t *testing.T) {
	observed := map[string]harness.ObservedItem{
		"text":    {ItemID: "text", Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, Text: "before"},
		"thought": {ItemID: "thought", Kind: eventpb.ItemKind_ITEM_KIND_REASONING, Text: "a\nb"},
		"call":    {ItemID: "call", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "commandExecution", Arguments: `{"command":"true","cwd":"/work"}`, Output: "output", Completed: true},
		"partial": {ItemID: "partial", Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, Text: "partial"},
	}
	adapter, host := newHistoryAdapter(observed,
		`"result":{"data":[{"turnId":"turn","item":{"type":"reasoning","id":"thought","summary":["a","b"]}}],"nextCursor":"opaque"}`,
		`"result":{"data":[{"turnId":"turn","item":{"type":"agentMessage","id":"text","text":"after"}},{"turnId":"turn","item":{"type":"commandExecution","id":"call","command":"true","cwd":"/work","status":"completed","aggregatedOutput":"output"}}],"nextCursor":null}`)
	report, err := adapter.Reconcile(context.Background(), "turn", false)
	if err != nil {
		t.Fatal(err)
	}
	assertDisposition(t, report, "text", eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED)
	assertDisposition(t, report, "thought", eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_RETAINED)
	assertDisposition(t, report, "call", eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_RETAINED)
	assertDisposition(t, report, "partial", eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT)
	if len(host.requests) != 2 {
		t.Fatalf("requests = %v", host.requests)
	}
	for _, req := range host.requests {
		if req.ThreadID != "thread" || req.TurnID == nil || *req.TurnID != "turn" || req.Limit == nil || *req.Limit != 100 {
			t.Fatalf("unbounded or incorrectly scoped history request: %+v", req)
		}
	}
	if host.requests[0].Cursor != nil || host.requests[1].Cursor == nil || host.requests[1].Cursor.String == nil || *host.requests[1].Cursor.String != "opaque" || host.requests[1].Cursor.ThreadItemsListAnchor != nil {
		t.Fatalf("cursor sequence = %v", host.requests)
	}
}

func TestUncertainHistoryNeverUsesPartialPagesAsAbsence(t *testing.T) {
	for _, test := range []struct {
		name  string
		pages []string
	}{
		{"unsupported", []string{`"error":{"code":-32601,"message":"unsupported store"}`}},
		{"missing data", []string{`"result":{}`}},
		{"malformed data", []string{`"result":{"data":42}`}},
		{"wrong turn", []string{`"result":{"data":[{"turnId":"other","item":{"id":"x","type":"agentMessage","text":"x"}}]}`}},
		{"missing identity", []string{`"result":{"data":[{"turnId":"turn","item":{"type":"agentMessage","text":"x"}}]}`}},
		{"empty cursor", []string{`"result":{"data":[],"nextCursor":""}`}},
		{"repeated cursor", []string{`"result":{"data":[],"nextCursor":"loop"}`, `"result":{"data":[],"nextCursor":"loop"}`}},
		{"later page error", []string{`"result":{"data":[],"nextCursor":"next"}`, `"error":{"code":-32603,"message":"failed page"}`}},
		{"duplicate item", []string{`"result":{"data":[{"turnId":"turn","item":{"id":"x","type":"reasoning"}},{"turnId":"turn","item":{"id":"x","type":"reasoning"}}]}`}},
		{"compaction", []string{`"result":{"data":[{"turnId":"turn","item":{"id":"compact","type":"contextCompaction"}}]}`}},
	} {
		t.Run(test.name, func(t *testing.T) {
			adapter, _ := newHistoryAdapter(map[string]harness.ObservedItem{"x": {ItemID: "x", Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, Text: "x"}}, test.pages...)
			report, err := adapter.Reconcile(context.Background(), "turn", false)
			if err != nil {
				t.Fatal(err)
			}
			assertDisposition(t, report, "x", eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN)
		})
	}
}

func TestLegacyAndUnspecifiedModesDoNotReadHistory(t *testing.T) {
	legacy := protocol.Legacy
	for _, mode := range []*protocol.ThreadHistoryMode{nil, &legacy} {
		adapter, host := newHistoryAdapter(map[string]harness.ObservedItem{"x": {ItemID: "x", Kind: eventpb.ItemKind_ITEM_KIND_REASONING}})
		adapter.historyMode = mode
		report, err := adapter.Reconcile(context.Background(), "turn", false)
		if err != nil {
			t.Fatal(err)
		}
		assertDisposition(t, report, "x", eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN)
		if len(host.requests) != 0 {
			t.Fatalf("unexpected legacy RPCs: %v", host.requests)
		}
	}
}

func TestEmptyTurnNeedsNoHistoryRPC(t *testing.T) {
	adapter, host := newHistoryAdapter(nil)
	report, err := adapter.Reconcile(context.Background(), "turn", false)
	if err != nil || len(report.Items) != 0 || len(host.requests) != 0 {
		t.Fatalf("report=%v requests=%v err=%v", report, host.requests, err)
	}
}

func TestIncompleteAndUnsupportedToolsRemainUnknown(t *testing.T) {
	adapter, _ := newHistoryAdapter(map[string]harness.ObservedItem{
		"running": {ItemID: "running", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "commandExecution"},
		"missing": {ItemID: "missing", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "commandExecution"},
		"failed":  {ItemID: "failed", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "commandExecution", Completed: true},
		"other":   {ItemID: "other", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "futureTool"},
	}, `"result":{"data":[{"turnId":"turn","item":{"id":"running","type":"commandExecution","status":"inProgress","aggregatedOutput":"partial"}},{"turnId":"turn","item":{"id":"failed","type":"commandExecution","status":"failed","aggregatedOutput":"partial","command":"sleep 100","cwd":"/work"}}]}`)
	report, err := adapter.Reconcile(context.Background(), "turn", false)
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"running", "missing", "failed", "other"} {
		assertDisposition(t, report, id, eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN)
	}
}

func TestResumeSharesBoundedSnapshotAndReadsEarlierItemOwner(t *testing.T) {
	adapter, host := newHistoryAdapter(map[string]harness.ObservedItem{"x": {ItemID: "x", TurnID: "owner", Kind: eventpb.ItemKind_ITEM_KIND_REASONING, Text: "kept"}},
		`"result":{"data":[{"turnId":"owner","item":{"id":"x","type":"reasoning","summary":["kept"]}}]}`,
		`"result":{"data":[]}`,
		`"result":{"data":[]}`)
	host.journal.turns = []string{"turn", "zzz-empty"}
	for range 2 {
		report, err := adapter.Reconcile(context.Background(), "turn", true)
		if err != nil {
			t.Fatal(err)
		}
		assertDisposition(t, report, "x", eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_RETAINED)
	}
	if len(host.requests) != 3 || *host.requests[0].TurnID != "owner" || *host.requests[1].TurnID != "turn" || *host.requests[2].TurnID != "zzz-empty" {
		t.Fatalf("requests should cover the owner and recovery window only, once: %v", host.requests)
	}
}

func TestLaterCompactionInvalidatesEarlierResumeTurn(t *testing.T) {
	adapter, host := newHistoryAdapter(map[string]harness.ObservedItem{"x": {ItemID: "x", Kind: eventpb.ItemKind_ITEM_KIND_REASONING, Text: "kept"}},
		`"result":{"data":[{"turnId":"later","item":{"id":"compact","type":"contextCompaction"}}]}`)
	host.journal.turns = []string{"turn", "later"}
	// The process can die before Agentplane observes the later turn's compaction item.
	host.journal.observed["later"] = nil
	report, err := adapter.Reconcile(context.Background(), "turn", true)
	if err != nil {
		t.Fatal(err)
	}
	assertDisposition(t, report, "x", eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN)
}

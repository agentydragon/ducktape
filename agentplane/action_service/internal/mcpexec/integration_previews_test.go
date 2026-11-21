package mcpexec

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// previewExecutor is the group's Executor over an MCP session with the server, which it serves over HTTP.
func previewExecutor(t *testing.T, group string, server *mcp.Server) *Executor {
	t.Helper()
	upstream := httptest.NewServer(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil))
	t.Cleanup(upstream.Close)
	client := mcp.NewClient(&mcp.Implementation{Name: "preview-client"}, nil)
	connected, err := client.Connect(t.Context(), &mcp.StreamableClientTransport{Endpoint: upstream.URL}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connected.Close() })
	return &Executor{key: group, available: true, current: &session{client: connected}}
}

func TestGmailThreadSummary(t *testing.T) {
	const raw = `{"id":"thread-1","messages":[{"snippet":"private body","payload":{"headers":[{"name":"sUbJeCt","value":"Deployment review"}]}}]}`
	var structured any
	if err := json.Unmarshal([]byte(raw), &structured); err != nil {
		t.Fatal(err)
	}
	for name, response := range map[string]*mcp.CallToolResult{
		"structured": {StructuredContent: structured},
		"text":       {Content: []mcp.Content{&mcp.TextContent{Text: raw}}},
	} {
		t.Run(name, func(t *testing.T) {
			got := gmailThreadSummary("thread-1", response)
			if got.GetThreadId() != "thread-1" || got.GetSubject() != "Deployment review" {
				t.Errorf("summary = %v", got)
			}
			if got := gmailThreadSummary("wrong-thread", response); got != nil {
				t.Errorf("mismatched thread produced %v", got)
			}
		})
	}
}

func TestTanaNodeSummary(t *testing.T) {
	response := &mcp.CallToolResult{Content: []mcp.Content{
		&mcp.TextContent{Text: "- Different node <!-- node-id: draft-2 -->\n  - [x] Draft notes <!-- node-id: draft -->\nPrivate description"},
	}}
	got := tanaNodeSummary("draft", response)
	if got.GetNodeId() != "draft" || got.GetName() != "Draft notes" {
		t.Errorf("summary = %v", got)
	}
	if got := tanaNodeSummary("not-returned", response); got != nil {
		t.Errorf("missing node produced %v", got)
	}
}

func TestTanaWorkspaceSummaryReturnsOnlyTheRequestedWorkspace(t *testing.T) {
	response := &mcp.CallToolResult{Content: []mcp.Content{
		&mcp.TextContent{Text: `[{"id":"other","name":"Unrelated","homeNodeId":"other-home"},{"id":"product","name":"Product","homeNodeId":"product-home"}]`},
	}}
	got := tanaWorkspaceSummary("product", response)
	if got.GetWorkspaceId() != "product" || got.GetName() != "Product" {
		t.Errorf("summary = %v", got)
	}
	if got := tanaWorkspaceSummary("missing", response); got != nil {
		t.Errorf("missing workspace produced %v", got)
	}
}

func TestTanaSummariesCallTheReadToolAndKeepPartialResults(t *testing.T) {
	server := mcp.NewServer(&mcp.Implementation{Name: "tana-preview-test"}, nil)
	server.AddTool(&mcp.Tool{Name: "read_node", InputSchema: json.RawMessage(`{
		"type": "object",
		"properties": {"nodeId": {"type": "string"}, "maxDepth": {"const": 0}},
		"required": ["nodeId", "maxDepth"],
		"additionalProperties": false
	}`)}, func(_ context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		var args struct {
			NodeID string `json:"nodeId"`
		}
		if err := json.Unmarshal(req.Params.Arguments, &args); err != nil {
			return nil, err
		}
		if args.NodeID != "draft" {
			return &mcp.CallToolResult{IsError: true}, nil
		}
		return &mcp.CallToolResult{Content: []mcp.Content{
			&mcp.TextContent{Text: "- Draft notes <!-- node-id: draft -->"},
		}}, nil
	})
	executor := previewExecutor(t, "tana", server)
	got := executor.TanaNodeSummaries(t.Context(), []string{"missing", "draft"})
	if len(got) != 1 || got[0].GetName() != "Draft notes" {
		t.Errorf("Tana summaries = %v", got)
	}
}

func TestCalendarEventPreviewProjectsOccurrenceMetadata(t *testing.T) {
	response := &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: `{
        "event_id":"occurrence-1",
        "summary":"Facilities coordination",
        "html_link":"https://calendar.google.com/calendar/event?eid=occurrence-1",
        "start":{"date_time":"2026-11-23T09:30:00-08:00","time_zone":"America/Los_Angeles"},
        "original_start_time":{"date_time":"2026-11-23T09:00:00-08:00","time_zone":"America/Los_Angeles"},
        "recurring_event_id":"series-1",
        "description":"Private description",
        "attendees":[{"email":"person@example.test"}]
    }`}}}
	got := googleCalendarEventPreview("occurrence-1", response)
	if got.GetEventId() != "occurrence-1" || got.GetSummary() != "Facilities coordination" ||
		got.GetScope().String() != "GOOGLE_CALENDAR_EVENT_SCOPE_OCCURRENCE" ||
		got.GetStart().GetTimeZone() != "America/Los_Angeles" ||
		got.GetOriginalStartTime().GetDateTime() != "2026-11-23T09:00:00-08:00" ||
		got.GetHtmlLink() != "https://calendar.google.com/calendar/event?eid=occurrence-1" {
		t.Errorf("Calendar event preview = %v", got)
	}
	if got := googleCalendarEventPreview("wrong-event", response); got != nil {
		t.Errorf("mismatched event produced %v", got)
	}
}

func TestGoogleCalendarSummaryReadsOnePageFromTheExactCalendar(t *testing.T) {
	calls := make(chan string, 4)
	server := mcp.NewServer(&mcp.Implementation{Name: "calendar-preview-test"}, nil)
	server.AddTool(&mcp.Tool{Name: "list_events", InputSchema: json.RawMessage(`{
		"type": "object",
		"properties": {"calendar_id": {"type": "string"}, "max_results": {"const": 1}},
		"required": ["calendar_id", "max_results"],
		"additionalProperties": false
	}`)}, func(_ context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		var args struct {
			CalendarID string `json:"calendar_id"`
		}
		if err := json.Unmarshal(req.Params.Arguments, &args); err != nil {
			return nil, err
		}
		select {
		case calls <- args.CalendarID:
		default:
			t.Error("unexpected calendar lookup")
		}
		var result string
		switch args.CalendarID {
		case "facilities@example.test":
			result = `{"summary":"Facilities calendar","events":[{"event_id":"evt1","summary":"Private event"}],"next_page_token":"unused"}`
		case "unnamed@example.test":
			result = `{"summary":null,"events":[]}`
		default:
			return &mcp.CallToolResult{IsError: true}, nil
		}
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: result}}}, nil
	})
	executor := previewExecutor(t, "google_calendar", server)
	got := executor.GoogleCalendarSummary(t.Context(), "facilities@example.test")
	if got.GetCalendarId() != "facilities@example.test" || got.GetSummary() != "Facilities calendar" {
		t.Errorf("calendar summary = %v", got)
	}
	for _, id := range []string{"unnamed@example.test", "missing@example.test"} {
		if got := executor.GoogleCalendarSummary(t.Context(), id); got != nil {
			t.Errorf("calendar %q should have no summary, got %v", id, got)
		}
	}
	if len(calls) != 3 {
		t.Errorf("provider received %d reads for three lookups", len(calls))
	}
}

func TestGrocySummariesMatchExactTypedReferences(t *testing.T) {
	result := &mcp.CallToolResult{StructuredContent: map[string]any{
		"result": []map[string]any{{"kind": "ok", "entity_type": "locations", "object_id": 7, "data": map[string]any{"name": "Pantry", "description": "internal"}}},
	}}
	if got := grocyEntitySummary("locations/7", result); got == nil || got.GetName() != "Pantry" || got.GetKey() != "locations/7" {
		t.Errorf("summary = %v", got)
	}
	for _, key := range []string{"products/7", "locations/8"} {
		if got := grocyEntitySummary(key, result); got != nil {
			t.Errorf("mismatched reference %q produced %v", key, got)
		}
	}
	entry := &mcp.CallToolResult{
		Content: []mcp.Content{&mcp.TextContent{Text: `[{"kind":"ok","entry":{"entry_id":12,"product_name":"Kefir","location_name":"Fridge"}}]`}},
	}
	if got := grocyStockEntrySummary("stock_entries/12", entry); got.GetName() != "Kefir · Fridge" {
		t.Errorf("entry summary = %v", got)
	}
}

func TestGmailMailLabelsRequireTheRecordedResourceInTheCurrentMailbox(t *testing.T) {
	server := mcp.NewServer(&mcp.Implementation{Name: "gmail-preview-test"}, nil)
	for _, name := range []string{"messages_get", "threads_get"} {
		server.AddTool(&mcp.Tool{Name: name, InputSchema: json.RawMessage(`{
			"type": "object",
			"properties": {"id": {"type": "string"}, "format": {"const": "minimal"}},
			"required": ["id", "format"],
			"additionalProperties": false
		}`)}, func(_ context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			var params struct{ ID string }
			if err := json.Unmarshal(req.Params.Arguments, &params); err != nil {
				return nil, err
			}
			if params.ID == "former-account" {
				return &mcp.CallToolResult{StructuredContent: map[string]any{"id": "other-message"}}, nil
			}
			return &mcp.CallToolResult{StructuredContent: map[string]any{"id": params.ID}}, nil
		})
	}
	labelCalls := make(chan string, 3)
	server.AddTool(&mcp.Tool{Name: "labels_get", InputSchema: json.RawMessage(`{
		"type": "object",
		"properties": {"id": {"type": "string"}},
		"required": ["id"],
		"additionalProperties": false
	}`)}, func(_ context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		var params struct{ ID string }
		if err := json.Unmarshal(req.Params.Arguments, &params); err != nil {
			return nil, err
		}
		select {
		case labelCalls <- params.ID:
		default:
			t.Error("unexpected label read")
		}
		return &mcp.CallToolResult{StructuredContent: map[string]any{"id": params.ID, "name": "Deployments/Reviewed"}}, nil
	})

	executor := previewExecutor(t, "gmail", server)
	for _, thread := range []bool{false, true} {
		got := executor.GmailMailLabelNames(t.Context(), "current-account", thread, []string{"Label_19"})
		if len(got) != 1 || got[0].GetTitle() != "Deployments/Reviewed" {
			t.Errorf("Gmail labels (thread=%v) = %v", thread, got)
		}
	}
	if got := executor.GmailMailLabelNames(t.Context(), "former-account", false, []string{"Label_19"}); len(got) != 0 {
		t.Errorf("receipt from former mailbox produced %v", got)
	}
	close(labelCalls)
	var ids []string
	for id := range labelCalls {
		ids = append(ids, id)
	}
	if len(ids) != 2 || ids[0] != "Label_19" || ids[1] != "Label_19" {
		t.Errorf("label reads = %q, want exactly two Label_19 reads", ids)
	}
}

func TestGrocyApprovalValuesFromExactRows(t *testing.T) {
	product := grocyEntitySummary("product_edits/2", textResultForPreview(`[
	  {"kind":"ok","entity_type":"products","object_id":2,"data":{
	    "name":"Rolled oats","location_id":"4","min_stock_amount":"0.5","default_best_before_days":"-1",
	    "due_type":1,"parent_product_id":null,"description":"Keep dry","calories":911
	  }}
	]`))
	if product == nil {
		t.Fatal("no product summary")
	}
	got := product.GetProductEditCurrent()
	if got.GetLocationId() != 4 || got.GetMinimumStock() != 0.5 ||
		got.GetShelfLifeDays() != -1 || got.GetParentProductId() != 0 ||
		got.GetDescription() != "Keep dry" || got.GetCalories() != 911 {
		t.Errorf("unexpected product values: %v", got)
	}
	if key := grocyProductLink(got, "location"); key != "locations/4" {
		t.Errorf("unexpected location reference: %q", key)
	}
	result := textResultForPreview(`[
	  {"kind":"ok","entity_type":"shopping_list","object_id":52,"data":{
	    "product_id":"2","shopping_list_id":7,"qu_id":4,"amount":"2.5","done":false,"note":"Plain"
	  },"shopping_item_names":{"product_name":"Rolled oats","list_name":"Weekly shop","unit_name":"Bag"}}
	]`)
	item := grocyEntitySummary("shopping_list/52", result)
	if item == nil {
		t.Fatal("no shopping item summary")
	}
	if item.GetName() != "Rolled oats" {
		t.Errorf("expected product name from the exact item lookup: %v", item)
	}
	current := item.GetShoppingItemCurrent()
	if current.GetAmount() != 2.5 || current.Done == nil || current.GetDone() || current.GetNote() != "Plain" ||
		current.GetListName() != "Weekly shop" || current.GetUnitName() != "Bag" {
		t.Errorf("unexpected shopping fields: %v", current)
	}
	if item := grocyEntitySummary("shopping_list/53", result); item != nil {
		t.Errorf("mismatched item returned %v", item)
	}
}

func TestGrocyProductEditResolvesChangedLinksInBatches(t *testing.T) {
	server := mcp.NewServer(&mcp.Implementation{Name: "grocy-product-preview-test"}, nil)
	calls := make(chan string, 32)
	server.AddTool(&mcp.Tool{Name: "entities_get", InputSchema: json.RawMessage(`{
		"type":"object",
		"properties":{"entity_type":{"type":"string"},"object_ids":{"type":"array","items":{"type":"integer"}}},
		"required":["entity_type","object_ids"]
	}`)}, func(_ context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		var args struct {
			EntityType string  `json:"entity_type"`
			ObjectIDs  []int64 `json:"object_ids"`
		}
		if err := json.Unmarshal(req.Params.Arguments, &args); err != nil {
			return nil, err
		}
		select {
		case calls <- fmt.Sprintf("%s:%d", args.EntityType, len(args.ObjectIDs)):
		default:
			return nil, fmt.Errorf("too many preview requests")
		}
		var rows []map[string]any
		for _, id := range args.ObjectIDs {
			data := map[string]any{"name": fmt.Sprintf("%s %d", args.EntityType, id)}
			switch args.EntityType {
			case "products":
				// The unrelated unit refs would have consumed the old lookup budget.
				data["location_id"] = id + 100
				data["qu_id_stock"] = 3 * id
				data["qu_id_purchase"] = 3*id + 1
				data["qu_id_consume"] = 3*id + 2
			case "locations":
				data["name"] = fmt.Sprintf("Cupboard %d", id)
			}
			rows = append(rows, map[string]any{"kind": "ok", "entity_type": args.EntityType, "object_id": id, "data": data})
		}
		return &mcp.CallToolResult{StructuredContent: map[string]any{"result": rows}}, nil
	})
	executor := previewExecutor(t, "grocy_sf", server)

	check := func(t *testing.T, count, firstChanged int) {
		var keys []string
		fields := make(map[string][]string)
		for id := 1; id <= count; id++ {
			key := fmt.Sprintf("product_edits/%d", id)
			keys = append(keys, key)
			if id >= firstChanged {
				fields[key] = []string{"location"}
			}
		}
		names := make(map[string]string)
		currentLocations := make(map[string]int64)
		for _, row := range executor.GrocyReferenceSummaries(t.Context(), keys, fields) {
			names[row.GetKey()] = row.GetName()
			currentLocations[row.GetKey()] = row.GetProductEditCurrent().GetLocationId()
		}
		for id, key := range keys {
			location := int64(id + 101)
			if id := currentLocations[key]; id != location {
				t.Errorf("%s location ID = %d, want %d", key, id, location)
			}
			if id+1 >= firstChanged {
				name := names[fmt.Sprintf("locations/%d", location)]
				if want := fmt.Sprintf("Cupboard %d", location); name != want {
					t.Errorf("%s location name = %q, want %q", key, name, want)
				}
			}
		}
		var actual []string
		for len(calls) > 0 {
			actual = append(actual, <-calls)
		}
		want := []string{fmt.Sprintf("products:%d", count), fmt.Sprintf("locations:%d", count-firstChanged+1)}
		if !slices.Equal(actual, want) {
			t.Errorf("MCP calls = %v, want %v", actual, want)
		}
	}
	t.Run("two name edits cannot crowd out third location", func(t *testing.T) { check(t, 3, 3) })
	t.Run("twenty location edits", func(t *testing.T) { check(t, 20, 1) })
}

func grocyEntitySummary(key string, result *mcp.CallToolResult) *operator_action_servicepb.GrocyReferenceSummary {
	summaries := grocyEntitySummaries([]string{key}, result)
	if len(summaries) == 1 {
		return summaries[0]
	}
	return nil
}

func textResultForPreview(text string) *mcp.CallToolResult {
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: text}}}
}

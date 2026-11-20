package mcpexec

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

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
	upstream := httptest.NewServer(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil))
	defer upstream.Close()
	client := mcp.NewClient(&mcp.Implementation{Name: "preview-client"}, nil)
	connected, err := client.Connect(t.Context(), &mcp.StreamableClientTransport{Endpoint: upstream.URL}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer connected.Close()
	executor := &Executor{key: "tana", available: true, current: &session{client: connected}}
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
	upstream := httptest.NewServer(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil))
	defer upstream.Close()
	client := mcp.NewClient(&mcp.Implementation{Name: "preview-client"}, nil)
	connected, err := client.Connect(t.Context(), &mcp.StreamableClientTransport{Endpoint: upstream.URL}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer connected.Close()
	executor := &Executor{key: "google_calendar", available: true, current: &session{client: connected}}
	got := executor.GoogleCalendarSummary(t.Context(), "facilities@example.test")
	if got.GetCalendarId() != "facilities@example.test" || got.GetSummary() != "Facilities calendar" {
		t.Errorf("calendar summary = %v", got)
	}
	for _, id := range []string{"unnamed@example.test", "missing@example.test"} {
		if got := executor.GoogleCalendarSummary(t.Context(), id); got != nil {
			t.Errorf("calendar %q should have no summary, got %v", id, got)
		}
	}
}

func TestGrocySummariesMatchExactTypedReferences(t *testing.T) {
	result := &mcp.CallToolResult{StructuredContent: map[string]any{
		"result": []map[string]any{{"kind": "ok", "entity_type": "locations", "object_id": 7, "data": map[string]any{"name": "Pantry", "description": "internal"}}},
	}}
	if got := grocyEntitySummary("locations/7", result); got.GetName() != "Pantry" || got.GetKey() != "locations/7" {
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
	upstream := httptest.NewServer(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil))
	defer upstream.Close()
	client := mcp.NewClient(&mcp.Implementation{Name: "preview-client"}, nil)
	connected, err := client.Connect(t.Context(), &mcp.StreamableClientTransport{Endpoint: upstream.URL}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer connected.Close()

	executor := &Executor{key: "gmail", available: true, current: &session{client: connected}}
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

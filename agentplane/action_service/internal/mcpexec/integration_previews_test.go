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

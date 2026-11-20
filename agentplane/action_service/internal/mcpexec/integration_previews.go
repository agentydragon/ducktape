package mcpexec

import (
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func (e *Executor) GmailThreadSummaries(ctx context.Context, ids []string) []*operator_action_servicepb.GmailThreadSummary {
	return readSummaries(ctx, e, ids, "gmail", "threads_get", func(id string) map[string]any {
		return map[string]any{"id": id, "format": "metadata"}
	}, gmailThreadSummary)
}

func (e *Executor) TanaNodeSummaries(ctx context.Context, ids []string) []*operator_action_servicepb.TanaNodeSummary {
	return readSummaries(ctx, e, ids, "tana", "read_node", func(id string) map[string]any {
		return map[string]any{"nodeId": id, "maxDepth": 0}
	}, tanaNodeSummary)
}

// list_workspaces takes no filter. Return metadata only for the workspace in the recorded request.
func (e *Executor) TanaWorkspaceSummary(ctx context.Context, id string) *operator_action_servicepb.TanaWorkspaceSummary {
	summaries := readSummaries(ctx, e, []string{id}, "tana", "list_workspaces", func(string) map[string]any {
		return map[string]any{}
	}, tanaWorkspaceSummary)
	if len(summaries) == 0 {
		return nil
	}
	return summaries[0]
}

func tanaWorkspaceSummary(id string, result *mcp.CallToolResult) *operator_action_servicepb.TanaWorkspaceSummary {
	if len(result.Content) != 1 {
		return nil
	}
	text, ok := result.Content[0].(*mcp.TextContent)
	if !ok {
		return nil
	}
	var workspaces []struct{ ID, Name string }
	if json.Unmarshal([]byte(text.Text), &workspaces) != nil {
		return nil
	}
	for _, workspace := range workspaces {
		if workspace.ID == id && strings.TrimSpace(workspace.Name) != "" {
			return &operator_action_servicepb.TanaWorkspaceSummary{WorkspaceId: id, Name: workspace.Name}
		}
	}
	return nil
}

func (e *Executor) GoogleCalendarEventPreview(ctx context.Context, calendarID, eventID string) *operator_action_servicepb.GoogleCalendarEventPreview {
	results := readSummaries(ctx, e, []string{eventID}, "google_calendar", "get_event", func(id string) map[string]any {
		return map[string]any{"calendar_id": calendarID, "event_id": id}
	}, googleCalendarEventPreview)
	if len(results) == 0 {
		return nil
	}
	return results[0]
}

type calendarEventTime struct {
	Date     string `json:"date"`
	DateTime string `json:"date_time"`
	TimeZone string `json:"time_zone"`
}

func (t *calendarEventTime) preview() *operator_action_servicepb.GoogleCalendarEventTime {
	if t == nil || (t.Date == "") == (t.DateTime == "") {
		return nil
	}
	return &operator_action_servicepb.GoogleCalendarEventTime{Date: t.Date, DateTime: t.DateTime, TimeZone: t.TimeZone}
}

func googleCalendarEventPreview(id string, result *mcp.CallToolResult) *operator_action_servicepb.GoogleCalendarEventPreview {
	var event struct {
		EventID           string             `json:"event_id"`
		Summary           string             `json:"summary"`
		HTMLLink          string             `json:"html_link"`
		Start             *calendarEventTime `json:"start"`
		End               *calendarEventTime `json:"end"`
		OriginalStartTime *calendarEventTime `json:"original_start_time"`
		Recurrence        []string           `json:"recurrence"`
		RecurringEventID  string             `json:"recurring_event_id"`
	}
	if !parsePreviewObject(result, &event) || event.EventID != id {
		return nil
	}
	scope := operator_action_servicepb.GoogleCalendarEventScope_GOOGLE_CALENDAR_EVENT_SCOPE_UNSPECIFIED
	start, end := event.Start.preview(), event.End.preview()
	switch {
	case event.RecurringEventID != "":
		scope = operator_action_servicepb.GoogleCalendarEventScope_GOOGLE_CALENDAR_EVENT_SCOPE_OCCURRENCE
	case len(event.Recurrence) != 0:
		scope = operator_action_servicepb.GoogleCalendarEventScope_GOOGLE_CALENDAR_EVENT_SCOPE_SERIES
	case start != nil && end != nil:
		scope = operator_action_servicepb.GoogleCalendarEventScope_GOOGLE_CALENDAR_EVENT_SCOPE_SINGLE_EVENT
	}
	return &operator_action_servicepb.GoogleCalendarEventPreview{
		EventId: id, Summary: event.Summary, HtmlLink: event.HTMLLink, Scope: scope,
		Start: start, End: end, OriginalStartTime: event.OriginalStartTime.preview(),
	}
}

func parsePreviewObject(result *mcp.CallToolResult, target any) bool {
	if result.StructuredContent != nil {
		payload, err := json.Marshal(result.StructuredContent)
		return err == nil && json.Unmarshal(payload, target) == nil
	}
	if len(result.Content) != 1 {
		return false
	}
	block, ok := result.Content[0].(*mcp.TextContent)
	return ok && json.Unmarshal([]byte(block.Text), target) == nil
}

// These operator lookups share the supervisor's MCP session. Each method fixes its provider and
// tool in code. A discovery annotation alone cannot authorize a tool call.
func readSummaries[T any](ctx context.Context, e *Executor, ids []string, group, name string, arguments func(string) map[string]any, summary func(string, *mcp.CallToolResult) *T) []*T {
	if e == nil || e.key != group || len(ids) == 0 {
		return nil
	}
	s := e.pin()
	if s == nil {
		return nil
	}
	defer s.calls.Done()
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	tools, _, err := e.list(ctx, s)
	tool, offered := tools[name]
	if err != nil || !offered {
		return nil
	}

	results := make([]*T, len(ids))
	var calls sync.WaitGroup
	for i, id := range ids {
		calls.Go(func() {
			args, err := json.Marshal(arguments(id))
			if err != nil || !tool.matches(args) {
				return
			}
			e.mu.Lock()
			usable := e.current == s && !s.failed && !e.draining
			e.mu.Unlock()
			if !usable || ctx.Err() != nil {
				return
			}
			result, err := s.client.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: json.RawMessage(args)})
			if err == nil && !result.IsError {
				results[i] = summary(id, result)
			}
		})
	}
	calls.Wait()
	summaries := make([]*T, 0, len(results))
	for _, result := range results {
		if result != nil {
			summaries = append(summaries, result)
		}
	}
	return summaries
}

func gmailThreadSummary(id string, result *mcp.CallToolResult) *operator_action_servicepb.GmailThreadSummary {
	var thread struct {
		ID       string
		Messages []struct {
			Payload struct {
				Headers []struct{ Name, Value string }
			}
		}
	}
	if !parsePreviewObject(result, &thread) || thread.ID != id || len(thread.Messages) == 0 {
		return nil
	}
	for _, header := range thread.Messages[0].Payload.Headers {
		if strings.EqualFold(header.Name, "Subject") && strings.TrimSpace(header.Value) != "" {
			return &operator_action_servicepb.GmailThreadSummary{ThreadId: id, Subject: header.Value}
		}
	}
	return nil
}

var (
	tanaNodeMarker = regexp.MustCompile(`(?m)^(.*?)[ \t]*<!-- node-id: ([^ \t\r\n]+) -->[ \t\r]*$`)
	tanaBullet     = regexp.MustCompile(`^[ \t]*(?:-[ \t]+)?(?:\[[ Xx]\](?:[ \t]+|$))?`)
)

func tanaNodeSummary(id string, result *mcp.CallToolResult) *operator_action_servicepb.TanaNodeSummary {
	for _, block := range result.Content {
		text, ok := block.(*mcp.TextContent)
		if !ok {
			continue
		}
		for _, match := range tanaNodeMarker.FindAllStringSubmatch(text.Text, -1) {
			if match[2] == id {
				name := strings.TrimSpace(tanaBullet.ReplaceAllString(match[1], ""))
				if name != "" {
					return &operator_action_servicepb.TanaNodeSummary{NodeId: id, Name: name}
				}
			}
		}
	}
	return nil
}

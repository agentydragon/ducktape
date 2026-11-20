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
	"google.golang.org/protobuf/encoding/protojson"
)

func (e *Executor) GmailThreadSummaries(ctx context.Context, ids []string) []*operator_action_servicepb.GmailThreadSummary {
	return readSummaries(ctx, e, ids, "gmail", "threads_get", func(id string) map[string]any {
		return map[string]any{"id": id, "format": "metadata"}
	}, gmailThreadSummary)
}

func (e *Executor) GmailDraftSubjects(ctx context.Context, ids []string) []*operator_action_servicepb.GmailResourceTitle {
	return readSummaries(ctx, e, ids, "gmail", "drafts_get", func(id string) map[string]any {
		return map[string]any{"id": id, "format": "metadata"}
	}, func(id string, result *mcp.CallToolResult) *operator_action_servicepb.GmailResourceTitle {
		var draft struct {
			ID      string
			Message gmailMessageHeaders
		}
		if !decodeGmailSummary(result, &draft) || draft.ID != id {
			return nil
		}
		return gmailResourceTitle(id, draft.Message.subject())
	})
}

func (e *Executor) GmailMessageSubjects(ctx context.Context, ids []string) []*operator_action_servicepb.GmailResourceTitle {
	return readSummaries(ctx, e, ids, "gmail", "messages_get", func(id string) map[string]any {
		return map[string]any{"id": id, "format": "metadata", "metadataHeaders": []string{"Subject"}}
	}, func(id string, result *mcp.CallToolResult) *operator_action_servicepb.GmailResourceTitle {
		var message struct {
			ID string
			gmailMessageHeaders
		}
		if !decodeGmailSummary(result, &message) || message.ID != id {
			return nil
		}
		return gmailResourceTitle(id, message.subject())
	})
}

func (e *Executor) GmailLabelNames(ctx context.Context, ids []string) []*operator_action_servicepb.GmailResourceTitle {
	return readSummaries(ctx, e, ids, "gmail", "labels_get", gmailIDArgument, gmailLabelName)
}

func gmailLabelName(id string, result *mcp.CallToolResult) *operator_action_servicepb.GmailResourceTitle {
	var label struct{ ID, Name string }
	if !decodeGmailSummary(result, &label) || label.ID != id {
		return nil
	}
	return gmailResourceTitle(id, label.Name)
}

// Label IDs are mailbox-local. Before resolving a saved receipt's IDs in the current Gmail
// mailbox, confirm that it contains the recorded message or thread, using the same MCP session.
func (e *Executor) GmailMailLabelNames(ctx context.Context, id string, thread bool, labelIDs []string) []*operator_action_servicepb.GmailResourceTitle {
	if e == nil || e.key != "gmail" || len(labelIDs) == 0 {
		return nil
	}
	s := e.pin()
	if s == nil {
		return nil
	}
	defer s.calls.Done()
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	// A linked token can change underneath a pinned MCP session. Discard metadata if its
	// authorization changed during this lookup.
	account, linked := linkedAccountOf(e.server)
	var initialToken string
	if linked {
		token, err := requestToken(ctx, e.linkages, account.serverID)
		if err != nil {
			return nil
		}
		initialToken = token
	}
	tools, _, err := e.list(ctx, s)
	if err != nil {
		return nil
	}
	name := "messages_get"
	if thread {
		name = "threads_get"
	}
	matches := readOfferedSummaries(ctx, e, s, tools, []string{id}, name, func(id string) map[string]any {
		return map[string]any{"id": id, "format": "minimal"}
	}, func(id string, result *mcp.CallToolResult) *string {
		var resource struct{ ID string }
		if !decodeGmailSummary(result, &resource) || resource.ID != id {
			return nil
		}
		return &resource.ID
	})
	if len(matches) != 1 {
		return nil
	}
	labels := readOfferedSummaries(ctx, e, s, tools, labelIDs, "labels_get", gmailIDArgument, gmailLabelName)
	if linked {
		currentToken, err := requestToken(ctx, e.linkages, account.serverID)
		if err != nil || currentToken != initialToken {
			return nil
		}
	}
	return labels
}

func (e *Executor) GmailFilters(ctx context.Context, ids []string) []*operator_action_servicepb.GmailFilterSummary {
	return readSummaries(ctx, e, ids, "gmail", "filters_get", gmailIDArgument,
		func(id string, result *mcp.CallToolResult) *operator_action_servicepb.GmailFilterSummary {
			filter := new(operator_action_servicepb.GmailFilterSummary)
			payload := gmailSummaryJSON(result)
			if protojson.Unmarshal(payload, filter) != nil ||
				filter.GetId() != id || filter.Criteria == nil || filter.Action == nil {
				return nil
			}
			return filter
		})
}

func gmailIDArgument(id string) map[string]any {
	return map[string]any{"id": id}
}

func gmailResourceTitle(id, title string) *operator_action_servicepb.GmailResourceTitle {
	if strings.TrimSpace(title) == "" {
		return nil
	}
	return &operator_action_servicepb.GmailResourceTitle{Id: id, Title: title}
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
	if err != nil {
		return nil
	}
	return readOfferedSummaries(ctx, e, s, tools, ids, name, arguments, summary)
}

func readOfferedSummaries[T any](ctx context.Context, e *Executor, s *session, tools map[string]tool, ids []string, name string, arguments func(string) map[string]any, summary func(string, *mcp.CallToolResult) *T) []*T {
	tool, offered := tools[name]
	if !offered {
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

type gmailMessageHeaders struct {
	Payload struct {
		Headers []struct{ Name, Value string }
	}
}

func (message gmailMessageHeaders) subject() string {
	for _, header := range message.Payload.Headers {
		if strings.EqualFold(header.Name, "Subject") && strings.TrimSpace(header.Value) != "" {
			return header.Value
		}
	}
	return ""
}

func gmailThreadSummary(id string, result *mcp.CallToolResult) *operator_action_servicepb.GmailThreadSummary {
	var thread struct {
		ID       string
		Messages []gmailMessageHeaders
	}
	if !decodeGmailSummary(result, &thread) || thread.ID != id || len(thread.Messages) == 0 {
		return nil
	}
	subject := thread.Messages[0].subject()
	if subject == "" {
		return nil
	}
	return &operator_action_servicepb.GmailThreadSummary{ThreadId: id, Subject: subject}
}

func decodeGmailSummary(result *mcp.CallToolResult, summary any) bool {
	return json.Unmarshal(gmailSummaryJSON(result), summary) == nil
}

func gmailSummaryJSON(result *mcp.CallToolResult) []byte {
	if result.StructuredContent != nil {
		payload, err := json.Marshal(result.StructuredContent)
		if err == nil {
			return payload
		}
		return nil
	}
	if len(result.Content) == 1 {
		if text, ok := result.Content[0].(*mcp.TextContent); ok {
			return []byte(text.Text)
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

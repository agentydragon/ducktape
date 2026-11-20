package rpc

import (
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/grpcgate"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
)

// IntegrationPreviews binds operator lookups to the configured providers.
type IntegrationPreviews struct {
	GoogleCalendar interface {
		GoogleCalendarEventPreview(context.Context, string, string) *operator_action_servicepb.GoogleCalendarEventPreview
	}
	Gmail interface {
		GmailThreadSummaries(context.Context, []string) []*operator_action_servicepb.GmailThreadSummary
	}
	Tana interface {
		TanaNodeSummaries(context.Context, []string) []*operator_action_servicepb.TanaNodeSummary
		TanaWorkspaceSummary(context.Context, string) *operator_action_servicepb.TanaWorkspaceSummary
	}
}

func (s *OperatorService) GetGmailThreadSummaries(ctx context.Context, request *operator_action_servicepb.GetGmailThreadSummariesRequest) (*operator_action_servicepb.GetGmailThreadSummariesResponse, error) {
	receipt, err := s.previewRequest(ctx, request.GetRequestId())
	if err != nil {
		return nil, err
	}
	if receipt.Action.Group != "gmail" {
		return nil, invalidRequest("request has no matching integration preview")
	}
	var ids []string
	switch receipt.Action.Name {
	case "threads_modify_labels", "drafts_create":
		var args struct {
			ThreadIDs []string `json:"thread_ids"`
			ThreadID  string   `json:"thread_id"`
		}
		if json.Unmarshal(receipt.Arguments, &args) != nil {
			return nil, invalidRequest("invalid Gmail preview arguments")
		}
		if receipt.Action.Name == "drafts_create" {
			ids = []string{args.ThreadID}
		} else {
			ids = args.ThreadIDs
		}
	case "threads_list":
		var list struct {
			Threads []struct{ ID string }
		}
		if recordedPreviewResult(receipt, &list) {
			for _, thread := range list.Threads {
				ids = append(ids, thread.ID)
			}
		}
	default:
		return nil, invalidRequest("request has no matching integration preview")
	}
	response := &operator_action_servicepb.GetGmailThreadSummariesResponse{}
	if s.previews.Gmail != nil {
		response.Threads = s.previews.Gmail.GmailThreadSummaries(ctx, previewIDs(ids))
	}
	return response, nil
}

func (s *OperatorService) GetTanaNodeSummaries(ctx context.Context, request *operator_action_servicepb.GetTanaNodeSummariesRequest) (*operator_action_servicepb.GetTanaNodeSummariesResponse, error) {
	recorded, err := s.previewRequest(ctx, request.GetRequestId())
	if err != nil {
		return nil, err
	}
	if recorded.Action.Group != "tana" {
		return nil, invalidRequest("request has no matching integration preview")
	}
	var args struct {
		NodeID          string   `json:"nodeId"`
		TargetNodeID    string   `json:"targetNodeId"`
		SourceParentID  string   `json:"sourceParentId"`
		ReferenceNodeID string   `json:"referenceNodeId"`
		ParentNodeID    string   `json:"parentNodeId"`
		WorkspaceID     string   `json:"workspaceId"`
		TagID           string   `json:"tagId"`
		TagIDs          []string `json:"tagIds"`
		ExtendsTagIDs   []string `json:"extendsTagIds"`
		SourceTagID     string   `json:"sourceTagId"`
		AttributeID     string   `json:"attributeId"`
		OptionID        string   `json:"optionId"`
		DoneMapping     struct {
			FieldID         string   `json:"fieldId"`
			CheckedValues   []string `json:"checkedValues"`
			UncheckedValues []string `json:"uncheckedValues"`
		} `json:"doneStateMapping"`
	}
	if json.Unmarshal(recorded.Arguments, &args) != nil {
		return nil, invalidRequest("request has invalid integration preview arguments")
	}

	var nodeIDs []string
	var workspaceID string
	switch recorded.Action.Name {
	case "move_node":
		nodeIDs = []string{args.NodeID, args.TargetNodeID, args.SourceParentID, args.ReferenceNodeID}
	case "get_or_create_calendar_node", "list_tags":
		workspaceID = args.WorkspaceID
	case "read_node", "get_children", "open_node", "edit_node", "trash_node", "check_node", "uncheck_node":
		nodeIDs = []string{args.NodeID}
	case "import_tana_paste":
		nodeIDs = []string{args.ParentNodeID}
	case "tag":
		nodeIDs = append([]string{args.NodeID}, args.TagIDs...)
	case "get_tag_schema":
		nodeIDs = []string{args.TagID}
	case "create_tag":
		workspaceID = args.WorkspaceID
		nodeIDs = args.ExtendsTagIDs
	case "add_field_to_tag":
		nodeIDs = []string{args.TagID, args.SourceTagID}
	case "set_tag_checkbox":
		nodeIDs = []string{args.TagID, args.DoneMapping.FieldID}
		nodeIDs = append(nodeIDs, args.DoneMapping.CheckedValues...)
		nodeIDs = append(nodeIDs, args.DoneMapping.UncheckedValues...)
	case "set_field_content":
		nodeIDs = []string{args.NodeID, args.AttributeID}
	case "set_field_option":
		nodeIDs = []string{args.NodeID, args.AttributeID, args.OptionID}
	default:
		return nil, invalidRequest("request has no matching integration preview")
	}
	response := &operator_action_servicepb.GetTanaNodeSummariesResponse{}
	if s.previews.Tana != nil {
		// Keep both optional reads within the preview's overall deadline, including any partial result.
		ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		var lookups sync.WaitGroup
		if previewID.MatchString(workspaceID) {
			lookups.Go(func() {
				response.Workspace = s.previews.Tana.TanaWorkspaceSummary(ctx, workspaceID)
			})
		}
		if ids := previewIDs(nodeIDs); len(ids) != 0 {
			lookups.Go(func() {
				response.Nodes = s.previews.Tana.TanaNodeSummaries(ctx, ids)
			})
		}
		lookups.Wait()
	}
	return response, nil
}

func (s *OperatorService) GetGoogleCalendarEventPreview(ctx context.Context, input *operator_action_servicepb.GetGoogleCalendarEventPreviewRequest) (*operator_action_servicepb.GetGoogleCalendarEventPreviewResponse, error) {
	request, err := s.previewRequest(ctx, input.GetRequestId())
	if err != nil {
		return nil, err
	}
	var args struct {
		CalendarID       json.RawMessage `json:"calendar_id"`
		EventID          string          `json:"event_id"`
		RecurringEventID string          `json:"recurring_event_id"`
	}
	if request.Action.Group != "google_calendar" || json.Unmarshal(request.Arguments, &args) != nil {
		return nil, invalidRequest("request has no matching Calendar preview")
	}
	var eventID string
	switch request.Action.Name {
	case "get_event", "update_event", "delete_event":
		eventID = args.EventID
	case "list_event_instances":
		eventID = args.RecurringEventID
	default:
		return nil, invalidRequest("request has no matching Calendar preview")
	}
	calendarID := "primary"
	if len(args.CalendarID) != 0 {
		var explicit *string
		if json.Unmarshal(args.CalendarID, &explicit) != nil || explicit == nil {
			return nil, invalidRequest("request has no valid calendar ID")
		}
		calendarID = *explicit
	}
	response := &operator_action_servicepb.GetGoogleCalendarEventPreviewResponse{}
	if !previewID.MatchString(eventID) || strings.TrimSpace(calendarID) == "" || len(calendarID) > 1024 {
		return nil, invalidRequest("request has no valid calendar or event ID")
	}
	if request.State == action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING && s.previews.GoogleCalendar != nil {
		response.Event = s.previews.GoogleCalendar.GoogleCalendarEventPreview(ctx, calendarID, eventID)
	}
	return response, nil
}

func (s *OperatorService) previewRequest(ctx context.Context, requestID string) (store.ActionRequest, error) {
	id, err := parseRequestID(requestID)
	if err != nil {
		return store.ActionRequest{}, err
	}
	return s.store.Get(ctx, id, grpcgate.Admitted(ctx).Principal)
}

// Read recorded, successful object results; the IDs never come from parameters to the summary RPC.
func recordedPreviewResult(receipt store.ActionRequest, result any) bool {
	if receipt.Execution == nil || receipt.Execution.State != store.ExecutionSucceeded {
		return false
	}
	var call struct {
		StructuredContent json.RawMessage
		Content           []struct{ Type, Text string }
		IsError           bool
	}
	if json.Unmarshal(receipt.Execution.Result, &call) != nil || call.IsError {
		return false
	}
	if call.StructuredContent != nil {
		return json.Unmarshal(call.StructuredContent, result) == nil
	}
	return len(call.Content) == 1 && call.Content[0].Type == "text" &&
		json.Unmarshal([]byte(call.Content[0].Text), result) == nil
}

var previewID = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,256}$`)

// A large receipt may contain more IDs. The caller shows the recorded ID for each omitted result.
func previewIDs(ids []string) []string {
	const limit = 8
	seen := make(map[string]bool, limit)
	selected := make([]string, 0, min(len(ids), limit))
	for _, id := range ids {
		if !seen[id] && previewID.MatchString(id) {
			seen[id] = true
			selected = append(selected, id)
			if len(selected) == limit {
				break
			}
		}
	}
	return selected
}

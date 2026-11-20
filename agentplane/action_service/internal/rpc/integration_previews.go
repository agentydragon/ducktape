package rpc

import (
	"context"
	"encoding/json"
	"regexp"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/grpcgate"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
)

// IntegrationPreviews binds operator lookups to the configured providers.
type IntegrationPreviews struct {
	Gmail interface {
		GmailThreadSummaries(context.Context, []string) []*operator_action_servicepb.GmailThreadSummary
	}
	Tana interface {
		TanaNodeSummaries(context.Context, []string) []*operator_action_servicepb.TanaNodeSummary
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
	var args struct {
		NodeID          string `json:"nodeId"`
		TargetNodeID    string `json:"targetNodeId"`
		SourceParentID  string `json:"sourceParentId"`
		ReferenceNodeID string `json:"referenceNodeId"`
	}
	if err := s.previewArguments(ctx, request.GetRequestId(), store.ActionIdentity{Group: "tana", Name: "move_node"}, &args); err != nil {
		return nil, err
	}
	response := &operator_action_servicepb.GetTanaNodeSummariesResponse{}
	if s.previews.Tana != nil {
		response.Nodes = s.previews.Tana.TanaNodeSummaries(ctx, previewIDs([]string{args.NodeID, args.TargetNodeID, args.SourceParentID, args.ReferenceNodeID}))
	}
	return response, nil
}

func (s *OperatorService) previewArguments(ctx context.Context, requestID string, action store.ActionIdentity, args any) error {
	request, err := s.previewRequest(ctx, requestID)
	if err != nil {
		return err
	}
	if request.Action != action || json.Unmarshal(request.Arguments, args) != nil {
		return invalidRequest("request has no matching integration preview")
	}
	return nil
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

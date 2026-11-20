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
	var args struct {
		ThreadIDs []string `json:"thread_ids"`
	}
	if err := s.previewArguments(ctx, request.GetRequestId(), store.ActionIdentity{Group: "gmail", Name: "threads_modify_labels"}, &args); err != nil {
		return nil, err
	}
	response := &operator_action_servicepb.GetGmailThreadSummariesResponse{}
	if s.previews.Gmail != nil {
		response.Threads = s.previews.Gmail.GmailThreadSummaries(ctx, previewIDs(args.ThreadIDs))
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
	id, err := parseRequestID(requestID)
	if err != nil {
		return err
	}
	request, err := s.store.Get(ctx, id, grpcgate.Admitted(ctx).Principal)
	if err != nil {
		return err
	}
	if request.Action != action || json.Unmarshal(request.Arguments, args) != nil {
		return invalidRequest("request has no matching integration preview")
	}
	return nil
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

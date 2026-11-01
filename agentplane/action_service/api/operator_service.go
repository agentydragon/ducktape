package api

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
)

// OperatorService is the OperatorActionService server.
type OperatorService struct {
	operator_action_servicepb.UnimplementedOperatorActionServiceServer
	store *store.Store
	// updates wakes a watch of the MCP servers on each commit to a linkage: it listens on linkage.UpdatesChannel.
	updates *wakeups.Hub
	// gate admitted each call; a watch asks it again before every frame.
	gate     *Gate
	linkages *linkage.Authority
}

func NewOperatorService(requests *store.Store, updates *wakeups.Hub, gate *Gate, linkages *linkage.Authority) *OperatorService {
	return &OperatorService{store: requests, updates: updates, gate: gate, linkages: linkages}
}

// DecideActionRequest records the Decision. The store queues an allowed request for dispatch in the same
// transaction, which wakes the dispatch worker.
func (s *OperatorService) DecideActionRequest(ctx context.Context, request *operator_action_servicepb.DecideActionRequestRequest) (*operator_action_servicepb.DecideActionRequestResponse, error) {
	id, err := parseRequestID(request.GetRequestId())
	if err != nil {
		return nil, err
	}
	verdict, known := storedVerdicts[request.GetVerdict()]
	if !known {
		return nil, invalidRequest("verdict %v is not a verdict", request.GetVerdict())
	}
	// The Gate admits only operators to this service.
	operator := admitted(ctx).principal.(store.OperatorPrincipal)
	decided, _, err := s.store.Decide(ctx, id, store.DecisionInput{
		Verdict: verdict, ExpectedVersion: request.GetExpectedVersion(), IdempotencyKey: request.GetIdempotencyKey(),
		DecisionNote: request.DecisionNote,
	}, operator)
	if err != nil {
		return nil, err
	}
	view, err := requestView(decided)
	if err != nil {
		return nil, err
	}
	return &operator_action_servicepb.DecideActionRequestResponse{ActionRequest: view}, nil
}

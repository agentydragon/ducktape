// Package api serves the Action Service's request RPCs over its store: `ActionService`'s reads and watches of
// requests and their events.
//
// A Gate admits every call before it runs (agentplane/docs/grpc_services.md). The bearer picks the principal, as
// action_service.proto says: a workload of a labeled ServiceAccount reads its own account's requests, a service
// reader every account's, and an operator every account's.
package api

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/google/uuid"
)

const (
	// maxListLimit bounds a page of ListActionRequests and of ListActionRequestEvents.
	maxListLimit = 128
	// defaultHistoryLimit and maxHistoryLimit bound a page of ListActionRequestHistory.
	defaultHistoryLimit = 50
	maxHistoryLimit     = 100
)

// Service is the ActionService server.
type Service struct {
	action_servicepb.UnimplementedActionServiceServer
	store *store.Store
	// updates wakes a watch on each commit to a request.
	updates *wakeups.Hub
	// gate admitted each call; a watch asks it again before every frame.
	gate *Gate
}

func NewService(requests *store.Store, updates *wakeups.Hub, gate *Gate) *Service {
	return &Service{store: requests, updates: updates, gate: gate}
}

func parseRequestID(value string) (uuid.UUID, error) {
	id, err := uuid.Parse(value)
	if err != nil {
		return uuid.Nil, invalidRequest("request_id must be a UUID")
	}
	return id, nil
}

// limit reads an optional page size, which must be 1 to most.
func limit(field string, value *int32, most int) (*int, error) {
	if value == nil {
		return nil, nil
	}
	if *value < 1 || int(*value) > most {
		return nil, invalidRequest("%s must be 1 to %d", field, most)
	}
	bound := int(*value)
	return &bound, nil
}

func (s *Service) ListActionRequests(ctx context.Context, request *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
	states, err := requestedStates(request.GetState())
	if err != nil {
		return nil, err
	}
	filter := store.ListFilter{States: states, IdempotencyKey: request.IdempotencyKey}
	if filter.Limit, err = limit("limit", request.Limit, maxListLimit); err != nil {
		return nil, err
	}
	if request.AfterId != nil {
		after, err := uuid.Parse(*request.AfterId)
		if err != nil {
			return nil, invalidRequest("after_id must be a UUID")
		}
		filter.AfterID = &after
	}
	requests, err := s.store.List(ctx, admitted(ctx).principal, filter)
	if err != nil {
		return nil, err
	}
	views, err := requestViews(requests)
	if err != nil {
		return nil, err
	}
	return &action_servicepb.ListActionRequestsResponse{ActionRequests: views}, nil
}

func (s *Service) ListActionRequestHistory(ctx context.Context, request *action_servicepb.ListActionRequestHistoryRequest) (*action_servicepb.ListActionRequestHistoryResponse, error) {
	page := store.HistoryPage{
		Limit: defaultHistoryLimit, Cursor: request.Cursor,
		// Requests a policy allowed are included unless the caller says otherwise.
		ExcludeAutoAllowed: request.IncludeAutoAllowed != nil && !*request.IncludeAutoAllowed,
	}
	bound, err := limit("limit", request.Limit, maxHistoryLimit)
	if err != nil {
		return nil, err
	}
	if bound != nil {
		page.Limit = *bound
	}
	requests, next, err := s.store.History(ctx, admitted(ctx).principal, page)
	if err != nil {
		return nil, err
	}
	views, err := requestViews(requests)
	if err != nil {
		return nil, err
	}
	return &action_servicepb.ListActionRequestHistoryResponse{ActionRequests: views, NextCursor: next}, nil
}

func (s *Service) GetActionRequest(ctx context.Context, request *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
	id, err := parseRequestID(request.GetRequestId())
	if err != nil {
		return nil, err
	}
	found, err := s.store.Get(ctx, id, admitted(ctx).principal)
	if err != nil {
		return nil, err
	}
	view, err := requestView(found)
	if err != nil {
		return nil, err
	}
	return &action_servicepb.GetActionRequestResponse{ActionRequest: view}, nil
}

func (s *Service) ListActionRequestEvents(ctx context.Context, request *action_servicepb.ListActionRequestEventsRequest) (*action_servicepb.ListActionRequestEventsResponse, error) {
	id, err := parseRequestID(request.GetRequestId())
	if err != nil {
		return nil, err
	}
	if request.GetAfterSequence() < 0 {
		return nil, invalidRequest("after_sequence must not be negative")
	}
	filter := store.EventsFilter{AfterSequence: int64(request.GetAfterSequence())}
	if filter.Limit, err = limit("limit", request.Limit, maxListLimit); err != nil {
		return nil, err
	}
	events, err := s.store.Events(ctx, id, admitted(ctx).principal, filter)
	if err != nil {
		return nil, err
	}
	views := make([]*action_servicepb.ActionEvent, len(events))
	for i, event := range events {
		if views[i], err = eventView(event); err != nil {
			return nil, err
		}
	}
	return &action_servicepb.ListActionRequestEventsResponse{Events: views}, nil
}

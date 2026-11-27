package rpc

import (
	"context"
	"log/slog"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/actionstate"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/grpcgate"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

// satisfies reads a watch's `until`: whether a receipt in a state ends the watch.
func satisfies(until action_servicepb.WatchUntil) (func(action_servicepb.ActionState) bool, error) {
	switch until {
	case action_servicepb.WatchUntil_WATCH_UNTIL_DECISION:
		return actionstate.Decided, nil
	case action_servicepb.WatchUntil_WATCH_UNTIL_UNSPECIFIED, action_servicepb.WatchUntil_WATCH_UNTIL_TERMINAL:
		return actionstate.Terminal, nil
	default:
		return nil, invalidRequest("until %v is not a watch condition", until)
	}
}

// follower is a watch's subscription to the changes of what it shows: the commits of requests or linkages, or the
// catalog's next change.
type follower struct {
	gate    *grpcgate.Gate
	updates <-chan struct{}
	// due fires when what the watch shows changes with the clock alone; nil when nothing does.
	due <-chan time.Time
}

// follow subscribes to the commits of requests. The watch reads after it, so a commit in between is not missed.
func (s *Service) follow() (follower, func(), error) {
	updates, unsubscribe, err := s.updates.Subscribe(store.UpdatesChannel)
	if err != nil {
		return follower{}, nil, err
	}
	return follower{gate: s.gate, updates: updates}, unsubscribe, nil
}

// changed blocks until a change may have changed what the watch shows: a wakeup, or due. The wakeup that follows a
// reconnection of the LISTEN connection is one, so a commit made while it was down is read late but not missed.
// Between changes it checks the bearer again every streaming.RecheckInterval. It fails once the bearer is no longer
// accepted, the service drains (grpcgate.ErrDraining), or the call ends.
func (f follower) changed(ctx context.Context) error {
	recheck := time.NewTicker(streaming.RecheckInterval)
	defer recheck.Stop()
	for {
		select {
		case <-f.updates:
			return nil
		case <-f.due:
			return nil
		case <-f.gate.DrainingContext().Done():
			return grpcgate.ErrDraining
		case <-ctx.Done():
			return ctx.Err()
		case <-recheck.C:
			if err := f.gate.Recheck(ctx); err != nil {
				return err
			}
		}
	}
}

// watchTimeoutMessage is the DEADLINE_EXCEEDED message of a watch write that outlasts streaming.WriteTimeout.
const watchTimeoutMessage = "action watch write deadline expired"

// send checks the bearer again, then sends the frame.
func send[Frame any](ctx, writeCtx context.Context, gate *grpcgate.Gate, stream grpc.ServerStreamingServer[Frame], frame *Frame) error {
	return streaming.SendWatchFrame(ctx, writeCtx, gate, stream.Send, frame, watchTimeoutMessage, grpcgate.ErrDraining)
}

func (s *Service) WatchActionRequest(request *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
	ctx := stream.Context()
	writeCtx, stopWrites := streaming.WatchWriteContext(ctx, s.gate)
	defer stopWrites()
	id, err := parseRequestID(request.GetRequestId())
	if err != nil {
		return err
	}
	satisfied, err := satisfies(request.GetUntil())
	if err != nil {
		return err
	}
	f, unsubscribe, err := s.follow()
	if err != nil {
		return err
	}
	defer unsubscribe()
	// Versions start at 1, so the first read is always sent.
	var sent int64
	for {
		found, err := s.store.Get(ctx, id, grpcgate.Admitted(ctx).Principal)
		if err != nil {
			return err
		}
		if found.Version != sent {
			view, err := requestView(found)
			if err != nil {
				return err
			}
			if err := send(ctx, writeCtx, s.gate, stream, &action_servicepb.WatchActionRequestResponse{ActionRequest: view}); err != nil {
				return err
			}
			sent = found.Version
		}
		if satisfied(found.State) {
			return nil
		}
		if err := f.changed(ctx); err != nil {
			return err
		}
	}
}

func (s *Service) WatchActionRequests(request *action_servicepb.WatchActionRequestsRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestsResponse]) error {
	ctx := stream.Context()
	states, err := requestedStates(request.GetState())
	if err != nil {
		return err
	}
	changes, unsubscribe, err := s.updates.SubscribeChanges(store.UpdatesChannel)
	if err != nil {
		return err
	}
	defer unsubscribe()
	principal := grpcgate.Admitted(ctx).Principal
	watch := streaming.VersionedSetWatch[store.ActionRequest, action_servicepb.WatchActionRequestsResponse]{
		Gate:           s.gate,
		Updates:        changes.Wakeups,
		Send:           stream.Send,
		TimeoutMessage: watchTimeoutMessage,
		Draining:       grpcgate.ErrDraining,
		List: func(ctx context.Context, after *store.ActionRequest, limit int) ([]store.ActionRequest, error) {
			page := store.ListFilter{States: states, Limit: &limit}
			if after != nil {
				page.AfterID = &after.ID
			}
			return s.store.List(ctx, principal, page)
		},
		Versions: func(ctx context.Context) (map[uuid.UUID]int64, error) {
			return s.store.Versions(ctx, principal, states)
		},
		// Every commit that changes a request's version notifies its ID.
		Changed: func() ([]uuid.UUID, bool) {
			keys, known := changes.Take()
			ids := make([]uuid.UUID, 0, len(keys))
			for _, key := range keys {
				id, err := uuid.Parse(key)
				if err != nil {
					slog.ErrorContext(ctx, "action update names no request; reading every version", "key", key, "error", err)
					return nil, false
				}
				ids = append(ids, id)
			}
			return ids, known
		},
		Find: func(ctx context.Context, ids []uuid.UUID) ([]store.ActionRequest, error) {
			return s.store.Find(ctx, principal, ids)
		},
		Key: func(request store.ActionRequest) streaming.Versioned {
			return streaming.Versioned{ID: request.ID, Version: request.Version, UpdatedAt: request.UpdatedAt}
		},
		Selects: func(request store.ActionRequest) bool {
			return len(states) == 0 || slices.Contains(states, request.State)
		},
		Snapshot: func(requests []store.ActionRequest) (*action_servicepb.WatchActionRequestsResponse, error) {
			views, err := requestViews(requests)
			if err != nil {
				return nil, err
			}
			return &action_servicepb.WatchActionRequestsResponse{Frame: &action_servicepb.WatchActionRequestsResponse_Snapshot{
				Snapshot: &action_servicepb.ActionRequestChunk{ActionRequests: views},
			}}, nil
		},
		SnapshotComplete: &action_servicepb.WatchActionRequestsResponse{
			Frame: &action_servicepb.WatchActionRequestsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}},
		},
		Upsert: func(request store.ActionRequest) (*action_servicepb.WatchActionRequestsResponse, error) {
			view, err := requestView(request)
			if err != nil {
				return nil, err
			}
			return &action_servicepb.WatchActionRequestsResponse{Frame: &action_servicepb.WatchActionRequestsResponse_Upsert{Upsert: view}}, nil
		},
		ReconnectRequired: &action_servicepb.WatchActionRequestsResponse{
			Frame: &action_servicepb.WatchActionRequestsResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
		},
	}
	return watch.Run(ctx)
}

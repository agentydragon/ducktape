package dispatcher

import (
	"context"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/resumetoken"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// subscribeWatch registers before the initial read, closing the subscribe/snapshot race.
// The same Hub and helper serve dispatch and Session watches. Reconnects coalesce into a
// wakeup, so the next version read covers commits made while LISTEN was disconnected.
func subscribeWatch(hub *wakeups.Hub, channels ...string) (<-chan struct{}, func(), error) {
	if hub == nil {
		return nil, nil, status.Error(codes.Unavailable, "dispatcher watch listener unavailable")
	}
	changed, unsubscribe, err := hub.Subscribe(channels...)
	if err != nil {
		return nil, nil, status.Error(codes.Unavailable, "dispatcher watch subscription unavailable")
	}
	return changed, unsubscribe, nil
}

func dispatchWatchFilter(filter *dispatcherpb.DispatchFilter) (*dispatcherpb.DispatchFilter, error) {
	if filter == nil || len(filter.GetCommandIds()) > maxFilterCommands {
		return nil, status.Error(codes.InvalidArgument, "dispatch filter is required and must name at most 1000 commands")
	}
	switch scope := filter.GetScope().(type) {
	case *dispatcherpb.DispatchFilter_SessionId:
		if scope.SessionId == "" {
			return nil, status.Error(codes.InvalidArgument, "session_id is required")
		}
	case *dispatcherpb.DispatchFilter_Sandbox:
		if scope.Sandbox == "" {
			return nil, status.Error(codes.InvalidArgument, "sandbox is required")
		}
	default:
		return nil, status.Error(codes.InvalidArgument, "dispatch filter scope is required")
	}
	canonical := proto.Clone(filter).(*dispatcherpb.DispatchFilter)
	slices.Sort(canonical.CommandIds)
	canonical.CommandIds = slices.Compact(canonical.CommandIds)
	return canonical, nil
}

func (s *Service) WatchDispatches(request *dispatcherpb.WatchDispatchesRequest, stream grpc.ServerStreamingServer[dispatcherpb.WatchDispatchesResponse]) error {
	ctx := stream.Context()
	caller, err := s.caller(ctx)
	if err != nil {
		return err
	}
	filter, err := dispatchWatchFilter(request.GetFilter())
	if err != nil {
		return err
	}
	filterBytes, err := wire(filter)
	if err != nil {
		return status.Error(codes.Internal, "cannot encode watch filter")
	}
	digest := resumetoken.Digest(append([]byte("dispatches\x00"), filterBytes...))
	var last int64
	resuming := request.GetResumeToken() != ""
	if resuming {
		last, err = resumetoken.Decode(request.GetResumeToken(), digest)
		if err != nil {
			return status.Error(codes.InvalidArgument, err.Error())
		}
	}
	changed, unsubscribe, err := subscribeWatch(s.Hub, NotifyChannel)
	if err != nil {
		return err
	}
	defer unsubscribe()
	if s.WatchLease <= 0 {
		return status.Error(codes.Internal, "watch lease must be positive")
	}
	leaseCtx, endLease := context.WithTimeout(ctx, s.WatchLease)
	defer endLease()
	first := true
	for {
		generation := s.Hub.Generation()
		batch, err := s.Store.readDispatchChanges(leaseCtx, filter, caller, last, resuming)
		if err != nil {
			if leaseCtx.Err() != nil && ctx.Err() == nil {
				return nil
			}
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return apiError(err)
		}
		for _, change := range batch.changes {
			ended, err := sendWatchDispatch(leaseCtx, ctx, stream, &dispatcherpb.WatchDispatchesResponse{
				Frame:       &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: change.dispatch},
				ResumeToken: resumetoken.Encode(change.version, digest),
			})
			if ended || err != nil {
				return err
			}
		}
		if first || batch.through > last {
			ended, err := sendWatchDispatch(leaseCtx, ctx, stream, &dispatcherpb.WatchDispatchesResponse{
				Frame:       &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}},
				ResumeToken: resumetoken.Encode(batch.through, digest),
			})
			if ended || err != nil {
				return err
			}
		}
		last, first, resuming = batch.through, false, true
		// Unlike Session snapshots, deletion coverage cannot be recovered by resetting
		// to zero. Keep the durable cursor on reconnect and validate its retention floor.
		if s.Hub.Generation() != generation {
			continue
		}
		select {
		case <-leaseCtx.Done():
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return nil
		case <-changed:
		}
	}
}

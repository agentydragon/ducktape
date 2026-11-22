package dispatcher

import (
	"context"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/resumetoken"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Subscribe before the initial read. Session-keyed notifications include deletion pruning.
// The Hub wakes both subscription types on a LISTEN disconnect/reconnect.
func subscribeDispatchWatch(hub *wakeups.Hub, filter *dispatcherpb.DispatchFilter) (<-chan struct{}, func(), error) {
	if hub == nil {
		return nil, nil, status.Error(codes.Unavailable, "dispatcher watch listener unavailable")
	}
	var changed <-chan struct{}
	var unsubscribe func()
	var err error
	if filter.GetRecentIssueLimit() != 0 {
		changed, unsubscribe, err = hub.SubscribeKey(store.NotifyChannel, filter.GetSessionId())
	} else {
		changed, unsubscribe, err = hub.Subscribe(store.NotifyChannel)
	}
	if err != nil {
		return nil, nil, status.Error(codes.Unavailable, "dispatcher watch subscription unavailable")
	}
	return changed, unsubscribe, nil
}

func dispatchWatchFilter(filter *dispatcherpb.DispatchFilter) (*dispatcherpb.DispatchFilter, error) {
	if filter == nil || len(filter.GetCommandIds()) > maxFilterCommands {
		return nil, status.Error(codes.InvalidArgument, "dispatch filter is required and must name at most 1000 commands")
	}
	if limit := filter.GetRecentIssueLimit(); limit != 0 {
		if limit > 100 || filter.GetSessionId() == "" || len(filter.GetCommandIds()) != 0 {
			return nil, status.Error(codes.InvalidArgument, "recent delivery issues require a whole Session and a limit of at most 100")
		}
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
	filterBytes, err := store.Wire(filter)
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
	changed, unsubscribe, err := subscribeDispatchWatch(s.Hub, filter)
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
	var lastIssues *dispatcherpb.RecentDispatchIssues
	issueGeneration := s.Hub.Generation()
	for {
		generation := s.Hub.Generation()
		if filter.GetRecentIssueLimit() != 0 && (!s.Hub.Connected() || generation != issueGeneration) {
			return status.Error(codes.Unavailable, "dispatcher delivery issue updates disconnected")
		}
		batch, err := s.Store.ReadDispatchChanges(leaseCtx, filter, caller, last, resuming)
		if err != nil {
			if leaseCtx.Err() != nil && ctx.Err() == nil {
				return nil
			}
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return apiError(err)
		}
		for _, change := range batch.Changes {
			ended, err := sendWatchDispatch(leaseCtx, ctx, stream, &dispatcherpb.WatchDispatchesResponse{
				Frame:       &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: change.Dispatch},
				ResumeToken: resumetoken.Encode(change.Version, digest),
			})
			if ended || err != nil {
				return err
			}
		}
		if batch.RecentIssues != nil {
			if !s.Hub.Connected() || s.Hub.Generation() != generation {
				return status.Error(codes.Unavailable, "dispatcher delivery issue updates disconnected")
			}
			if !proto.Equal(lastIssues, batch.RecentIssues) {
				ended, err := sendWatchDispatch(leaseCtx, ctx, stream, &dispatcherpb.WatchDispatchesResponse{
					Frame:       &dispatcherpb.WatchDispatchesResponse_RecentIssues{RecentIssues: batch.RecentIssues},
					ResumeToken: resumetoken.Encode(batch.Through, digest),
				})
				if ended || err != nil {
					return err
				}
				lastIssues = batch.RecentIssues
			}
		} else if first || batch.Through > last {
			ended, err := sendWatchDispatch(leaseCtx, ctx, stream, &dispatcherpb.WatchDispatchesResponse{
				Frame:       &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}},
				ResumeToken: resumetoken.Encode(batch.Through, digest),
			})
			if ended || err != nil {
				return err
			}
		}
		last, first, resuming = batch.Through, false, true
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

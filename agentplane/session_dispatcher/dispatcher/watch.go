package dispatcher

import (
	"context"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/resumetoken"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// watchWriteTimeout bounds how long a slow reader can hold a Dispatcher watch.
const watchWriteTimeout = 5 * time.Second

func sendWatchDispatch(
	ctx context.Context,
	stream grpc.ServerStreamingServer[dispatcherpb.WatchDispatchesResponse],
	frame *dispatcherpb.WatchDispatchesResponse,
) error {
	return streaming.SendWithinStatus(ctx, watchWriteTimeout, stream.Send, frame, "dispatcher watch write deadline expired")
}

// subscribeDispatches registers before the initial read. A reconnect wakes both
// kinds of subscription so each watch can handle a gap in notifications.
func (s *Service) subscribeDispatches(sessionID string) (<-chan struct{}, func(), error) {
	if s.Hub == nil {
		return nil, nil, status.Error(codes.Unavailable, "dispatcher watch listener unavailable")
	}
	var changed <-chan struct{}
	var unsubscribe func()
	var err error
	if sessionID == "" {
		changed, unsubscribe, err = s.Hub.Subscribe(store.NotifyChannel)
	} else {
		changed, unsubscribe, err = s.Hub.SubscribeKey(store.NotifyChannel, sessionID)
	}
	if err != nil {
		return nil, nil, status.Error(codes.Unavailable, "dispatcher watch subscription unavailable")
	}
	return changed, unsubscribe, nil
}

func dispatchFilter(filter *dispatcherpb.DispatchFilter) (*dispatcherpb.DispatchFilter, error) {
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
	if sessionID := request.GetWorksetSessionId(); sessionID != "" {
		if request.GetFilter() != nil {
			return status.Error(codes.InvalidArgument, "dispatch workset cannot be combined with a filter")
		}
		return s.watchDispatchWorkset(ctx, caller, sessionID, request.GetResumeToken(), stream)
	}
	filter, err := dispatchFilter(request.GetFilter())
	if err != nil {
		return err
	}
	filterBytes, err := store.Wire(filter)
	if err != nil {
		return status.Error(codes.Internal, "cannot encode watch filter")
	}
	digest := resumetoken.Digest(append([]byte("dispatches\x00"), filterBytes...))
	last, err := resumetoken.Decode(request.GetResumeToken(), digest)
	if err != nil {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	resuming := request.GetResumeToken() != ""
	// Notifications carry the Session ID; only Sandbox scope needs every key.
	changed, unsubscribe, err := s.subscribeDispatches(filter.GetSessionId())
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
		batch, err := s.Store.ReadDispatchChanges(leaseCtx, filter, caller, last, resuming)
		if err != nil {
			return watchEnd(ctx, leaseCtx, apiError(err))
		}
		for _, change := range batch.Changes {
			if err := sendWatchDispatch(leaseCtx, stream, &dispatcherpb.WatchDispatchesResponse{
				Frame:       &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: change.Dispatch},
				ResumeToken: resumetoken.Encode(change.Version, digest),
			}); err != nil {
				return watchEnd(ctx, leaseCtx, err)
			}
		}
		if first || batch.Through > last {
			if err := sendWatchDispatch(leaseCtx, stream, &dispatcherpb.WatchDispatchesResponse{
				Frame:       &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}},
				ResumeToken: resumetoken.Encode(batch.Through, digest),
			}); err != nil {
				return watchEnd(ctx, leaseCtx, err)
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
			return ctx.Err()
		case <-changed:
		}
	}
}

// watchEnd treats lease expiry as a clean end after a read or a write fails. The
// caller starts a fresh authorized watch. A cancelled RPC keeps its status.
func watchEnd(ctx, leaseCtx context.Context, err error) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if leaseCtx.Err() != nil {
		return nil
	}
	return err
}

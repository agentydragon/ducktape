package dispatcher

import (
	"context"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/resumetoken"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func (s *Service) watchDispatchWorkset(
	ctx context.Context,
	caller store.Caller,
	sessionID string,
	token string,
	stream grpc.ServerStreamingServer[dispatcherpb.WatchDispatchesResponse],
) error {
	digest := resumetoken.Digest([]byte("dispatch-workset\x00" + sessionID))
	after, err := resumetoken.Decode(token, digest)
	if err != nil {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	changed, unsubscribe, err := s.subscribeDispatches(sessionID)
	if err != nil {
		return err
	}
	defer unsubscribe()
	if s.WatchLease <= 0 {
		return status.Error(codes.Internal, "watch lease must be positive")
	}
	leaseCtx, endLease := context.WithTimeout(ctx, s.WatchLease)
	defer endLease()

	resuming := token != ""
	generation := s.Hub.Generation()
	var previous *dispatcherpb.DispatchWorkset
	for {
		if !s.Hub.Connected() || generation != s.Hub.Generation() {
			return status.Error(codes.Unavailable, "dispatcher workset updates disconnected")
		}
		workset, through, err := s.Store.ReadDispatchWorkset(leaseCtx, sessionID, caller, after, resuming)
		if err != nil {
			return watchEnd(ctx, leaseCtx, apiError(err))
		}
		if !s.Hub.Connected() || generation != s.Hub.Generation() {
			return status.Error(codes.Unavailable, "dispatcher workset updates disconnected")
		}
		if !proto.Equal(previous, workset) {
			if err := sendWatchDispatch(leaseCtx, stream, &dispatcherpb.WatchDispatchesResponse{
				Frame:       &dispatcherpb.WatchDispatchesResponse_Workset{Workset: workset},
				ResumeToken: resumetoken.Encode(through, digest),
			}); err != nil {
				return watchEnd(ctx, leaseCtx, err)
			}
			previous = workset
		}
		after, resuming = through, true
		select {
		case <-leaseCtx.Done():
			return ctx.Err()
		case <-changed:
		}
	}
}

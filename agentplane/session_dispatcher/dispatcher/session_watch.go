package dispatcher

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/resumetoken"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

const SessionNotifyChannel = "agentplane_dispatcher_session"

// Session notifications carry only a routing key, never Session data or instructions.
func sessionWatchKey(caller Caller) string {
	if identity, ok := caller.(IdentityCaller); ok {
		return "identity:" + identity.Identity.GetName() + ":" + identity.Identity.GetUid()
	}
	return "all"
}

func sessionWatchDigest(caller Caller) [32]byte {
	scope, args := ownerScope(caller, "", 1)
	// SQL and its scalar arguments are the complete ListSessions filter. Service actors with
	// the same read scope share tokens; delegating to another Identity changes the digest.
	data, err := json.Marshal(struct {
		Resource string
		Scope    string
		Args     []any
	}{"sessions", scope, args})
	if err != nil {
		panic(err)
	} // ownerScope only emits strings and booleans.
	return resumetoken.Digest(data)
}

func (s *Store) sessionsAfter(ctx context.Context, version int64, caller Caller) ([]*Session, error) {
	scope, args := ownerScope(caller, "", 2)
	rows, err := s.DB.QueryContext(ctx, "SELECT "+sessionColumns+
		" FROM session WHERE version > $1 AND "+scope+" ORDER BY version LIMIT 100", append([]any{version}, args...)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var sessions []*Session
	for rows.Next() {
		session, err := scanSession(rows)
		if err != nil {
			return nil, err
		}
		sessions = append(sessions, session)
	}
	return sessions, rows.Err()
}

func (s *Service) WatchSessions(request *dispatcherpb.WatchSessionsRequest, stream grpc.ServerStreamingServer[dispatcherpb.WatchSessionsResponse]) error {
	ctx := stream.Context()
	caller, err := s.caller(ctx)
	if err != nil {
		return err
	}
	digest := sessionWatchDigest(caller)
	last, err := resumetoken.Decode(request.GetResumeToken(), digest)
	if err != nil {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	if s.Hub == nil {
		return status.Error(codes.Unavailable, "Session watch listener unavailable")
	}
	generation := s.Hub.Generation()
	// Subscribe before reading so a commit cannot fall between the read and subscription.
	changed, unsubscribe, err := s.Hub.SubscribeKey(SessionNotifyChannel, sessionWatchKey(caller))
	if err != nil {
		return status.Error(codes.Unavailable, "Session watch listener unavailable")
	}
	defer unsubscribe()
	if s.WatchLease <= 0 {
		return status.Error(codes.Internal, "watch lease must be positive")
	}
	leaseCtx, endLease := context.WithTimeout(ctx, s.WatchLease)
	defer endLease()
	var high int64
	if err := s.Store.DB.QueryRowContext(leaseCtx, "SELECT version FROM session_watch_version WHERE singleton").Scan(&high); err != nil {
		return sessionWatchEnd(ctx, leaseCtx, apiError(err))
	}
	if last > high {
		return status.Error(codes.InvalidArgument, "watch resume version is ahead of stored Sessions")
	}
	caughtUp := false
	send := func(sendCtx context.Context, frame *dispatcherpb.WatchSessionsResponse) error {
		frame.ResumeToken = resumetoken.Encode(last, digest)
		err := streaming.SendWithin(sendCtx, watchWriteTimeout, stream.Send, frame)
		if errors.Is(err, streaming.ErrSendTimeout) {
			return status.Error(codes.DeadlineExceeded, "Session watch write deadline expired")
		}
		return err
	}
	for {
		if current := s.Hub.Generation(); current != generation {
			generation, last, caughtUp = current, 0, false
		}
		if s.Hub.Connected() {
			sessions, err := s.Store.sessionsAfter(leaseCtx, last, caller)
			if err != nil {
				return sessionWatchEnd(ctx, leaseCtx, apiError(err))
			}
			for _, session := range sessions {
				resource, err := DescribeSession(session)
				if err != nil {
					return apiError(err)
				}
				last = session.Version
				if err := send(leaseCtx, &dispatcherpb.WatchSessionsResponse{Frame: &dispatcherpb.WatchSessionsResponse_Session{Session: resource}}); err != nil {
					return sessionWatchEnd(ctx, leaseCtx, err)
				}
			}
			if len(sessions) > 0 {
				continue
			}
			if !caughtUp && s.Hub.Connected() && s.Hub.Generation() == generation {
				if err := send(leaseCtx, &dispatcherpb.WatchSessionsResponse{Frame: &dispatcherpb.WatchSessionsResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}}}); err != nil {
					return sessionWatchEnd(ctx, leaseCtx, err)
				}
				caughtUp = true
			}
		}
		select {
		case <-leaseCtx.Done():
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return send(ctx, &dispatcherpb.WatchSessionsResponse{Frame: &dispatcherpb.WatchSessionsResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}})
		case <-changed:
		}
	}
}

func sessionWatchEnd(ctx, leaseCtx context.Context, err error) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if leaseCtx.Err() != nil {
		return nil
	}
	return err
}

package api

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

const openingsPage = 500

type watchStream = grpc.ServerStreamingServer[historypb.WatchSessionSummariesResponse]

// WatchSessionSummaries sends `changed` on opening and after each change, each Session recorded since when the
// request asks for them, and `reconnect_required` when the lease ends or the server drains while idle. If either
// ends during a write, the handler returns without another send. A wakeup only prompts the caller to re-read: what
// it follows is in the tables, so a notification lost on a replica or a dropped LISTEN connection costs at most a
// spurious `changed`.
func (s *Service) WatchSessionSummaries(request *historypb.WatchSessionSummariesRequest, stream watchStream) error {
	ctx := stream.Context()
	if err := s.admit(ctx); err != nil {
		return err
	}
	leased, endLease := context.WithTimeout(ctx, s.lease)
	defer endLease()
	defer context.AfterFunc(s.draining, endLease)()
	// Subscribed before the opening notice, so nothing committed after it goes unannounced. A recorded Session
	// also wakes a stream that asked for none: a spurious `changed` is harmless.
	changed, unsubscribe, err := s.wakeups.Subscribe(projection.ChannelSummaries, storage.ChannelSessions)
	if err != nil {
		return failure(ctx, err)
	}
	defer unsubscribe()
	openedAfter := request.OpenedAfter
	for {
		if err := s.send(leased, stream, &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_Changed{Changed: &emptypb.Empty{}}}); err != nil {
			return err
		}
		if openedAfter != nil {
			position, err := s.announceOpenings(leased, stream, *openedAfter)
			if err != nil {
				return err
			}
			openedAfter = &position
		}
		select {
		case <-changed:
		case <-leased.Done():
			if ctx.Err() != nil {
				return status.FromContextError(ctx.Err()).Err()
			}
			// The lease ended or the server is draining; the caller connects again with fresh authorization.
			return s.send(ctx, stream, &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}})
		}
	}
}

// admit authenticates under the request timeout; the stream itself carries no deadline.
func (s *Service) admit(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, s.requestTimeout)
	defer cancel()
	return s.authenticate(ctx)
}

// announceOpenings sends `session_opened` for each Session recorded after the position, and returns the last
// position sent.
func (s *Service) announceOpenings(ctx context.Context, stream watchStream, after uint64) (uint64, error) {
	for {
		page, err := s.openedSessions(ctx, after)
		if err != nil {
			return 0, err
		}
		for _, opened := range page {
			response := &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_SessionOpened{
				SessionOpened: &historypb.SessionOpened{SessionId: opened.SessionID.String(), Position: opened.Position},
			}}
			if err := s.send(ctx, stream, response); err != nil {
				return 0, err
			}
			after = opened.Position
		}
		if len(page) < openingsPage {
			return after, nil
		}
	}
}

func (s *Service) openedSessions(ctx context.Context, after uint64) ([]storage.OpenedSession, error) {
	ctx, cancel := context.WithTimeout(ctx, s.requestTimeout)
	defer cancel()
	page, err := s.reader.History.OpenedSessions(ctx, after, openingsPage)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return page, nil
}

// send writes within the request timeout: a caller that stops reading would otherwise hold the stream open
// past its lease.
func (s *Service) send(ctx context.Context, stream watchStream, response *historypb.WatchSessionSummariesResponse) error {
	return sendWithin(ctx, stream, s.requestTimeout, response)
}

func sendWithin[Response any](ctx context.Context, stream interface{ Send(*Response) error }, timeout time.Duration, response *Response) error {
	err := streaming.SendWithin(ctx, timeout, stream.Send, response)
	switch {
	case errors.Is(err, streaming.ErrSendTimeout):
		return status.Error(codes.DeadlineExceeded, "history stream write deadline expired")
	case errors.Is(err, context.Canceled):
		return status.FromContextError(context.Canceled).Err()
	case errors.Is(err, context.DeadlineExceeded):
		return status.FromContextError(context.DeadlineExceeded).Err()
	default:
		return err
	}
}

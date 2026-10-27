package api

import (
	"context"
	"errors"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

type followStream = grpc.ServerStreamingServer[historypb.FollowSessionResponse]

func (s *Service) FollowSession(request *historypb.FollowSessionRequest, stream followStream) error {
	ctx := stream.Context()
	if err := s.admit(ctx); err != nil {
		return err
	}
	sessionID, err := uuid.Parse(request.GetSessionId())
	if err != nil {
		return status.Error(codes.InvalidArgument, "invalid session ID")
	}
	leased, endLease := context.WithTimeout(ctx, s.lease)
	defer endLease()
	defer context.AfterFunc(s.draining, endLease)()
	// Subscribe before the opening snapshot: every intervening commit leaves a pending re-read.
	changed, unsubscribe, err := s.wakeups.Subscribe(storage.ChannelHistory)
	if err != nil {
		return failure(ctx, err)
	}
	defer unsubscribe()
	readCtx, cancel := context.WithTimeout(leased, s.requestTimeout)
	after, err := s.reader.History.ResolveFollowStart(readCtx, sessionID, request)
	if err != nil {
		err = failure(readCtx, err)
	}
	cancel()
	if err != nil {
		return err
	}
	call := &followCall{service: s, stream: stream, sessionID: sessionID, request: request, through: after}
	initial := true
	for {
		if leased.Err() != nil {
			return call.reconnect()
		}
		if err := call.catchUp(leased, initial); err != nil {
			var writeErr *terminalWriteError
			if errors.As(err, &writeErr) {
				return writeErr.err
			}
			if leased.Err() != nil && ctx.Err() == nil {
				return call.reconnect()
			}
			return err
		}
		initial = false
		if !request.GetFollow() {
			return nil
		}
		select {
		case <-changed:
		case <-leased.Done():
			return call.reconnect()
		}
	}
}

type followCall struct {
	service   *Service
	stream    followStream
	sessionID uuid.UUID
	request   *historypb.FollowSessionRequest
	through   uint64
}

type terminalWriteError struct{ err error }

func (e *terminalWriteError) Error() string { return e.err.Error() }
func (e *terminalWriteError) Unwrap() error { return e.err }

func (c *followCall) send(ctx context.Context, response *historypb.FollowSessionResponse) error {
	response.ThroughCursor = c.through
	return c.write(ctx, response)
}

func (c *followCall) write(ctx context.Context, response *historypb.FollowSessionResponse) error {
	if err := sendWithin(ctx, c.stream, c.service.requestTimeout, response); err != nil {
		return &terminalWriteError{err: err}
	}
	return nil
}

func (c *followCall) reconnect() error {
	if err := c.stream.Context().Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	err := c.send(c.stream.Context(), &historypb.FollowSessionResponse{Frame: &historypb.FollowSessionResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}})
	var writeErr *terminalWriteError
	if errors.As(err, &writeErr) {
		return writeErr.err
	}
	return err
}

func (c *followCall) catchUp(ctx context.Context, initial bool) error {
	startThrough := c.through
	sentFrame := false
	for {
		readCtx, cancel := context.WithTimeout(ctx, c.service.requestTimeout)
		page, err := c.service.reader.History.ReadFollowEntries(readCtx, c.sessionID, c.through, c.request.GetKinds(), wantsSettlements(c.request.GetKinds()))
		if err != nil {
			err = failure(readCtx, err)
		}
		cancel()
		if err != nil {
			return err
		}
		for _, settled := range page.Settlements {
			if err := c.send(ctx, &historypb.FollowSessionResponse{Frame: &historypb.FollowSessionResponse_Settled{Settled: settled}}); err != nil {
				return err
			}
			sentFrame = true
		}
		if len(page.Entries) > 0 {
			response := &historypb.FollowSessionResponse{ThroughCursor: page.ThroughCursor, Frame: &historypb.FollowSessionResponse_Entries{Entries: &historypb.Entries{Entries: page.Entries}}}
			if err := c.write(ctx, response); err != nil {
				return err
			}
			sentFrame = true
		}
		c.through = page.ThroughCursor
		if c.through == page.LastCursor {
			break
		}
	}
	readCtx, cancel := context.WithTimeout(ctx, c.service.requestTimeout)
	removed, err := c.service.reader.History.ReadFollowRemoved(readCtx, c.sessionID, 0, c.through)
	if err != nil {
		err = failure(readCtx, err)
	}
	cancel()
	if err != nil {
		return err
	}
	for _, cursorRange := range removed {
		if err := c.send(ctx, &historypb.FollowSessionResponse{Frame: &historypb.FollowSessionResponse_Removed{Removed: cursorRange}}); err != nil {
			return err
		}
		sentFrame = true
	}
	if initial || c.through != startThrough || sentFrame {
		return c.send(ctx, &historypb.FollowSessionResponse{Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
	}
	return nil
}

func wantsSettlements(kinds []historypb.EventKind) bool {
	if len(kinds) == 0 {
		return true
	}
	for _, kind := range kinds {
		switch kind {
		case historypb.EventKind_EVENT_KIND_TEXT_DELTA, historypb.EventKind_EVENT_KIND_TOOL_ARGUMENTS_DELTA,
			historypb.EventKind_EVENT_KIND_TOOL_OUTPUT_DELTA, historypb.EventKind_EVENT_KIND_NATIVE:
			return true
		}
	}
	return false
}

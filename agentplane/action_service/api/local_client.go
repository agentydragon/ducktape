package api

import (
	"context"
	"errors"
	"io"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// CallerResolver authenticates an in-process caller. The core knows only the ServiceAccount it proves;
// the MCP edge owns the bearer and its Connection. ResolveCaller must consult current authority on each call.
type CallerResolver interface {
	ResolveCaller(context.Context) (auth.Account, error)
}

// LocalClient reaches the same servicer as gRPC, with the Gate's admission, drain and error handling.
// It is private to the process: no network metadata can install its caller or its recheck function.
type LocalClient struct {
	server  action_servicepb.ActionServiceServer
	gate    *Gate
	callers CallerResolver
}

var _ action_servicepb.ActionServiceClient = (*LocalClient)(nil)

func NewLocalClient(server action_servicepb.ActionServiceServer, gate *Gate, callers CallerResolver) *LocalClient {
	return &LocalClient{server: server, gate: gate, callers: callers}
}

func (c *LocalClient) admit(ctx context.Context) (context.Context, error) {
	return c.gate.admit(ctx, func(ctx context.Context) (admission, error) {
		account, err := c.callers.ResolveCaller(ctx)
		if err != nil {
			return admission{}, err
		}
		if !c.gate.callers.Admits(account) {
			return admission{}, unauthenticated
		}
		return admission{
			principal: store.CallerPrincipal{Account: store.ServiceAccount{Namespace: account.Namespace, Name: account.Name}},
			recheck: func(ctx context.Context) error {
				current, err := c.callers.ResolveCaller(ctx)
				if err != nil {
					return err
				}
				if current != account || !c.gate.callers.Admits(current) {
					return unauthenticated
				}
				return nil
			},
		}, nil
	})
}

func localCall[Request, Response any](c *LocalClient, ctx context.Context, request *Request,
	call func(context.Context, *Request) (*Response, error),
) (*Response, error) {
	admitted, err := c.admit(ctx)
	if err != nil {
		return nil, failure(ctx, err)
	}
	response, err := call(admitted, request)
	return response, failure(admitted, err)
}

func (c *LocalClient) SubmitActionRequest(ctx context.Context, request *action_servicepb.SubmitActionRequestRequest, _ ...grpc.CallOption) (*action_servicepb.SubmitActionRequestResponse, error) {
	return localCall(c, ctx, request, c.server.SubmitActionRequest)
}

func (c *LocalClient) ListActionRequests(ctx context.Context, request *action_servicepb.ListActionRequestsRequest, _ ...grpc.CallOption) (*action_servicepb.ListActionRequestsResponse, error) {
	return localCall(c, ctx, request, c.server.ListActionRequests)
}

func (c *LocalClient) ListActionRequestHistory(ctx context.Context, request *action_servicepb.ListActionRequestHistoryRequest, _ ...grpc.CallOption) (*action_servicepb.ListActionRequestHistoryResponse, error) {
	return localCall(c, ctx, request, c.server.ListActionRequestHistory)
}

func (c *LocalClient) GetActionRequest(ctx context.Context, request *action_servicepb.GetActionRequestRequest, _ ...grpc.CallOption) (*action_servicepb.GetActionRequestResponse, error) {
	return localCall(c, ctx, request, c.server.GetActionRequest)
}

func (c *LocalClient) WatchActionRequest(ctx context.Context, request *action_servicepb.WatchActionRequestRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[action_servicepb.WatchActionRequestResponse], error) {
	return localWatch(c, ctx, request, c.server.WatchActionRequest)
}

func (c *LocalClient) WatchActionRequests(ctx context.Context, request *action_servicepb.WatchActionRequestsRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[action_servicepb.WatchActionRequestsResponse], error) {
	return localWatch(c, ctx, request, c.server.WatchActionRequests)
}

func (c *LocalClient) CancelActionRequest(ctx context.Context, request *action_servicepb.CancelActionRequestRequest, _ ...grpc.CallOption) (*action_servicepb.CancelActionRequestResponse, error) {
	return localCall(c, ctx, request, c.server.CancelActionRequest)
}

func (c *LocalClient) ListActionRequestEvents(ctx context.Context, request *action_servicepb.ListActionRequestEventsRequest, _ ...grpc.CallOption) (*action_servicepb.ListActionRequestEventsResponse, error) {
	return localCall(c, ctx, request, c.server.ListActionRequestEvents)
}

func (c *LocalClient) ListActionGroups(ctx context.Context, request *action_servicepb.ListActionGroupsRequest, _ ...grpc.CallOption) (*action_servicepb.ListActionGroupsResponse, error) {
	return localCall(c, ctx, request, c.server.ListActionGroups)
}

func (c *LocalClient) WatchActionGroups(ctx context.Context, request *action_servicepb.WatchActionGroupsRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[action_servicepb.WatchActionGroupsResponse], error) {
	return localWatch(c, ctx, request, c.server.WatchActionGroups)
}

func (c *LocalClient) GetAction(ctx context.Context, request *action_servicepb.GetActionRequest, _ ...grpc.CallOption) (*action_servicepb.GetActionResponse, error) {
	return localCall(c, ctx, request, c.server.GetAction)
}

func (c *LocalClient) GetActionPolicy(ctx context.Context, request *action_servicepb.GetActionPolicyRequest, _ ...grpc.CallOption) (*action_servicepb.GetActionPolicyResponse, error) {
	return localCall(c, ctx, request, c.server.GetActionPolicy)
}

// localWatch carries typed frames with backpressure. Cancellation releases a blocked sender; closing
// frames publishes the terminal status to Recv, after all preceding frames have been consumed.
func localWatch[Request, Frame any](c *LocalClient, ctx context.Context, request *Request,
	call func(*Request, grpc.ServerStreamingServer[Frame]) error,
) (grpc.ServerStreamingClient[Frame], error) {
	admitted, err := c.admit(ctx)
	if err != nil {
		return nil, failure(ctx, err)
	}
	stream := &localStream[Frame]{ctx: admitted, frames: make(chan *Frame)}
	go func() {
		stream.err = failure(admitted, call(request, stream))
		close(stream.frames)
	}()
	return stream, nil
}

type localStream[Frame any] struct {
	ctx    context.Context
	frames chan *Frame
	err    error
}

func (s *localStream[Frame]) Context() context.Context { return s.ctx }
func (s *localStream[Frame]) Send(frame *Frame) error {
	select {
	case s.frames <- frame:
		return nil
	case <-s.ctx.Done():
		return s.ctx.Err()
	}
}

func (s *localStream[Frame]) Recv() (*Frame, error) {
	select {
	case frame, open := <-s.frames:
		if open {
			return frame, nil
		}
		if s.err != nil {
			return nil, s.err
		}
		return nil, io.EOF
	case <-s.ctx.Done():
		return nil, failure(s.ctx, s.ctx.Err())
	}
}

// The ActionService servicers use typed frames, not transport metadata or untyped messages.
// Fail explicitly if that contract grows instead of silently discarding transport operations.
var errLocalTransport = errors.New("in-process ActionService does not support transport metadata or untyped messages")

func (s *localStream[Frame]) Header() (metadata.MD, error) { return nil, errLocalTransport }
func (s *localStream[Frame]) Trailer() metadata.MD         { return nil }
func (s *localStream[Frame]) CloseSend() error             { return nil }
func (s *localStream[Frame]) SendMsg(any) error            { return errLocalTransport }
func (s *localStream[Frame]) RecvMsg(any) error            { return errLocalTransport }
func (s *localStream[Frame]) SetHeader(metadata.MD) error  { return errLocalTransport }
func (s *localStream[Frame]) SendHeader(metadata.MD) error { return errLocalTransport }
func (s *localStream[Frame]) SetTrailer(metadata.MD)       { panic(errLocalTransport) }

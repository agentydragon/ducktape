package gateway

import (
	"context"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"
)

type sendServiceFrame func(*runnercontrolpb.ServiceFrame) error

type tunnelResult struct {
	reply *tunnelpb.Reply
}

// tunnel carries concurrent unary Runner calls over one runner-initiated stream. It deliberately
// does not retry: when a stream ends, the outcome of every unanswered call is unknown.
type tunnel struct {
	send sendServiceFrame

	writeMu  sync.Mutex
	mu       sync.Mutex
	nextID   atomic.Uint64
	pending  map[uint64]chan tunnelResult
	closed   chan struct{}
	closeMu  sync.Mutex
	closeErr error
}

func newTunnel(send sendServiceFrame) *tunnel {
	return &tunnel{send: send, pending: make(map[uint64]chan tunnelResult), closed: make(chan struct{})}
}

func (t *tunnel) sendFrame(frame *runnercontrolpb.ServiceFrame) error {
	t.writeMu.Lock()
	defer t.writeMu.Unlock()
	select {
	case <-t.closed:
		return t.closedError()
	default:
	}
	if err := t.send(frame); err != nil {
		t.close(status.Error(codes.Unavailable, "runner control stream ended while sending"))
		return status.Error(codes.Unavailable, "runner control stream ended while sending")
	}
	return nil
}

func (t *tunnel) call(ctx context.Context, method string, request, response proto.Message, opts ...grpc.CallOption) error {
	encoded, err := proto.Marshal(request)
	if err != nil {
		return status.Error(codes.Internal, "could not encode runner request")
	}
	id := t.nextID.Add(1)
	call := &tunnelpb.Call{CallId: id, Method: method, Request: encoded, Traceparent: observability.Traceparent(ctx)}
	if deadline, ok := ctx.Deadline(); ok {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return status.Error(codes.DeadlineExceeded, "deadline exceeded before runner call")
		}
		call.Timeout = durationpb.New(remaining)
	}
	result := make(chan tunnelResult, 1)
	t.mu.Lock()
	select {
	case <-t.closed:
		t.mu.Unlock()
		return t.closedError()
	default:
	}
	t.pending[id] = result
	t.mu.Unlock()
	defer func() {
		t.mu.Lock()
		delete(t.pending, id)
		t.mu.Unlock()
	}()
	if err := t.sendFrame(&runnercontrolpb.ServiceFrame{Frame: &runnercontrolpb.ServiceFrame_Call{Call: call}}); err != nil {
		return err
	}
	select {
	case received := <-result:
		if received.reply == nil {
			return status.Error(codes.Unavailable, "runner control stream ended before the runner answered")
		}
		trailer := make(metadata.MD)
		for _, item := range received.reply.TrailingMetadata {
			if item == nil {
				continue
			}
			switch value := item.Value.(type) {
			case *tunnelpb.Metadata_Text:
				trailer.Append(item.Key, value.Text)
			case *tunnelpb.Metadata_Binary:
				trailer.Append(item.Key, string(value.Binary))
			}
		}
		for _, option := range opts {
			switch typed := option.(type) {
			case grpc.HeaderCallOption:
				if typed.HeaderAddr != nil {
					*typed.HeaderAddr = metadata.MD{}
				}
			case grpc.TrailerCallOption:
				if typed.TrailerAddr != nil {
					*typed.TrailerAddr = trailer.Copy()
				}
			}
		}
		if len(trailer) > 0 {
			_ = grpc.SetTrailer(ctx, trailer)
		}
		if code := codes.Code(received.reply.Code); code != codes.OK {
			return status.Error(code, received.reply.Details)
		}
		if err := proto.Unmarshal(received.reply.Response, response); err != nil {
			return status.Error(codes.Internal, "runner returned an invalid response")
		}
		return nil
	case <-t.closed:
		return t.closedError()
	case <-ctx.Done():
		return status.FromContextError(ctx.Err()).Err()
	}
}

func (t *tunnel) answer(reply *tunnelpb.Reply) {
	t.mu.Lock()
	waiting := t.pending[reply.CallId]
	t.mu.Unlock()
	if waiting == nil {
		return
	}
	select {
	case waiting <- tunnelResult{reply: reply}:
	default:
	}
}

func (t *tunnel) close(err error) {
	t.closeMu.Lock()
	defer t.closeMu.Unlock()
	select {
	case <-t.closed:
		return
	default:
		t.closeErr = err
		close(t.closed)
	}
}

func (t *tunnel) closedError() error {
	t.closeMu.Lock()
	defer t.closeMu.Unlock()
	if t.closeErr != nil {
		return t.closeErr
	}
	return status.Error(codes.Unavailable, "runner control stream ended")
}

// tunnelClientConn lets the generated Runner client use this authenticated runner
// stream as its transport. State observations use reverse control heartbeats.
type tunnelClientConn struct {
	tunnel *tunnel
}

func (c *tunnelClientConn) Invoke(ctx context.Context, method string, args, reply any, opts ...grpc.CallOption) error {
	request, okay := args.(proto.Message)
	if !okay {
		return status.Error(codes.Internal, "Runner request is not a protobuf message")
	}
	response, okay := reply.(proto.Message)
	if !okay {
		return status.Error(codes.Internal, "Runner response is not a protobuf message")
	}
	return c.tunnel.call(ctx, method, request, response, opts...)
}

func (*tunnelClientConn) NewStream(context.Context, *grpc.StreamDesc, string, ...grpc.CallOption) (grpc.ClientStream, error) {
	return nil, status.Error(codes.Unimplemented, "streaming Runner RPCs require forward transport")
}

var _ grpc.ClientConnInterface = (*tunnelClientConn)(nil)

package control

import (
	"context"
	"errors"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner/stream"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	heartbeatInterval = 10 * time.Second
	streamLiveness    = 30 * time.Second
)

// Endpoint is one authenticated Runner Gateway destination.
type Endpoint struct {
	Target string
	Bearer string
}

// Dependencies are the Runner-owned operations used by the control connection.
type Dependencies struct {
	BootID           string
	Server           runnerservicepb.RunnerServer
	AdoptIncarnation func(context.Context, string) error
	Facts            func() (*controlpb.RunnerFacts, error)
	Changes          func() <-chan struct{}
	StopRetry        func(error) bool
}

// Client owns the Runner Gateway control stream and its reconnect lifecycle.
type Client struct {
	bootID            string
	server            runnerservicepb.RunnerServer
	adoptIncarnation  func(context.Context, string) error
	facts             func() (*controlpb.RunnerFacts, error)
	changes           func() <-chan struct{}
	stopRetry         func(error) bool
	heartbeatInterval time.Duration
	streamLiveness    time.Duration
}

func New(dependencies Dependencies) (*Client, error) {
	if dependencies.BootID == "" || dependencies.Server == nil || dependencies.AdoptIncarnation == nil || dependencies.Facts == nil || dependencies.Changes == nil {
		return nil, errors.New("invalid Runner control dependencies")
	}
	return &Client{
		bootID:            dependencies.BootID,
		server:            dependencies.Server,
		adoptIncarnation:  dependencies.AdoptIncarnation,
		facts:             dependencies.Facts,
		changes:           dependencies.Changes,
		stopRetry:         dependencies.StopRetry,
		heartbeatInterval: heartbeatInterval,
		streamLiveness:    streamLiveness,
	}, nil
}

// Run keeps the authenticated control stream connected until ctx ends or a local operation fails.
func (c *Client) Run(ctx context.Context, endpoint Endpoint) error {
	return stream.Retry(ctx, "control", func(ctx context.Context) (bool, error) {
		return c.connect(ctx, endpoint)
	}, c.stopRetry)
}

func (c *Client) connect(ctx context.Context, endpoint Endpoint) (bool, error) {
	conn, err := stream.NewConnection(endpoint.Target)
	if err != nil {
		return false, err
	}
	defer conn.Close()
	ctx, cancel := context.WithCancel(ctx)
	var receiving, calls, adoption sync.WaitGroup
	defer func() {
		cancel()
		calls.Wait()
		receiving.Wait()
		adoption.Wait()
	}()
	controlStream, err := controlpb.NewRunnerControlClient(conn).Connect(stream.Authenticated(ctx, endpoint.Bearer))
	if err != nil {
		return false, err
	}
	var sending sync.Mutex
	send := func(frame *controlpb.RunnerFrame) error {
		sending.Lock()
		defer sending.Unlock()
		return controlStream.Send(frame)
	}
	if err := send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Hello{Hello: &controlpb.Hello{RunnerBootId: c.bootID}}}); err != nil {
		return false, err
	}
	type incoming struct {
		frame *controlpb.ServiceFrame
		err   error
	}
	received := make(chan incoming, 1)
	receiving.Add(1)
	go func() {
		defer receiving.Done()
		for {
			frame, err := controlStream.Recv()
			select {
			case received <- incoming{frame, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	liveness := time.NewTimer(c.streamLiveness)
	defer liveness.Stop()
	var first incoming
	select {
	case first = <-received:
	case <-liveness.C:
		return false, errors.New("no Welcome from Runner Gateway")
	case <-ctx.Done():
		return false, ctx.Err()
	}
	if first.err != nil {
		return false, first.err
	}
	welcome := first.frame.GetWelcome()
	if welcome == nil || welcome.IncarnationUid == "" {
		return false, status.Error(codes.InvalidArgument, "expected Welcome naming the Sandbox incarnation")
	}
	// Adoption may wait for the previous generation to flush. Keep the stream
	// live, but do not publish its facts or invoke Runner methods until it ends.
	adopted := make(chan error, 1)
	adoption.Add(1)
	go func() {
		defer adoption.Done()
		adopted <- c.adoptIncarnation(ctx, welcome.IncarnationUid)
	}()
	var started time.Time
	streamStable := func() bool { return !started.IsZero() && time.Since(started) >= c.streamLiveness }
	lastReceived := time.Now()
	ticker := time.NewTicker(c.heartbeatInterval)
	defer ticker.Stop()
	answers := make(chan error, 16)
	var published *controlpb.RunnerFacts
	incarnationAdopted := false
	for {
		var changed <-chan struct{}
		if incarnationAdopted {
			changed = c.changes()
			facts, err := c.facts()
			if err != nil {
				return false, err
			}
			if !proto.Equal(facts, published) {
				if err := send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Heartbeat{Heartbeat: &controlpb.RunnerHeartbeat{Facts: facts}}}); err != nil {
					return streamStable(), err
				}
				published = facts
			}
		}
		select {
		case <-ctx.Done():
			return false, ctx.Err()
		case err := <-adopted:
			if err != nil {
				return false, err
			}
			if err := ctx.Err(); err != nil {
				return false, err
			}
			incarnationAdopted = true
			started = time.Now()
		case err := <-answers:
			if err != nil {
				return streamStable(), err
			}
		case message := <-received:
			if message.err != nil {
				return streamStable(), message.err
			}
			lastReceived = time.Now()
			if call := message.frame.GetCall(); call != nil {
				if !incarnationAdopted {
					reply := &tunnelpb.Reply{CallId: call.CallId, Code: int32(codes.Unavailable), Details: "runner is adopting the Sandbox incarnation"}
					if err := send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Reply{Reply: reply}}); err != nil {
						return false, err
					}
					continue
				}
				calls.Add(1)
				go func() {
					defer calls.Done()
					reply := c.relay(ctx, call)
					err := send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Reply{Reply: reply}})
					select {
					case answers <- err:
					case <-ctx.Done():
					}
				}()
			} else if message.frame.GetHeartbeat() == nil {
				return false, status.Error(codes.InvalidArgument, "unexpected control frame")
			}
		case <-ticker.C:
			if time.Since(lastReceived) > c.streamLiveness {
				return streamStable(), errors.New("no frame from Runner Gateway")
			}
			if err := send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Heartbeat{Heartbeat: &controlpb.RunnerHeartbeat{}}}); err != nil {
				return false, err
			}
		case <-changed:
		}
	}
}

// relay uses the generated service descriptor's decoder and handler, preserving
// the generated request types and gRPC errors without a second RPC dispatcher.
func (c *Client) relay(ctx context.Context, call *tunnelpb.Call) *tunnelpb.Reply {
	traceparent := call.GetTraceparent()
	if traceparent != "" {
		ctx = metadata.NewIncomingContext(ctx, metadata.Pairs("traceparent", traceparent))
		ctx = observability.WithTraceparent(ctx, traceparent)
	}
	ctx, span := observability.Start(ctx, "Runner RPC")
	defer span.End()
	if call.Timeout != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, call.Timeout.AsDuration())
		defer cancel()
	}
	reply := &tunnelpb.Reply{CallId: call.CallId}
	name, ok := strings.CutPrefix(call.Method, "/"+runnerservicepb.Runner_ServiceDesc.ServiceName+"/")
	if !ok {
		reply.Code = int32(codes.Unimplemented)
		reply.Details = "unknown Runner method"
		return reply
	}
	for _, method := range runnerservicepb.Runner_ServiceDesc.Methods {
		if method.MethodName != name {
			continue
		}
		response, err := method.Handler(c.server, ctx, func(value any) error { return proto.Unmarshal(call.Request, value.(proto.Message)) }, nil)
		if err == nil {
			reply.Response, err = proto.Marshal(response.(proto.Message))
		}
		if err != nil {
			state := status.Convert(err)
			reply.Code = int32(state.Code())
			reply.Details = state.Message()
		}
		return reply
	}
	reply.Code = int32(codes.Unimplemented)
	reply.Details = "unknown Runner method"
	return reply
}

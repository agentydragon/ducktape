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
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	runnertransport "github.com/agentydragon/ducktape/agentplane/runner_transport"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	heartbeatInterval   = 10 * time.Second
	controlWriteTimeout = 30 * time.Second
	streamLiveness      = 30 * time.Second
)

// Endpoint is one authenticated Runner Gateway destination.
type Endpoint struct {
	Target string
	TLS    runnertransport.Config
}

// ExecutorConnection owns the Runner's executor channels for one control connection.
// Route must not wait for executor startup or I/O. Close joins workers after context cancellation.
type ExecutorConnection interface {
	Route(*executorpb.ExecutorFrame) error
	Close()
}

// Dependencies are the Runner-owned operations used by the control connection.
type Dependencies struct {
	BootID           string
	Server           runnerservicepb.RunnerServer
	AdoptIncarnation func(context.Context, string) error
	Facts            func() (*controlpb.RunnerFacts, error)
	Changes          func() <-chan struct{}
	StopRetry        func(error) bool
	OpenExecutor     func(context.Context, uint64, func(*controlpb.RunnerFrame) error, func(*controlpb.RunnerFrame) error) ExecutorConnection
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
	writeTimeout      time.Duration
	openExecutor      func(context.Context, uint64, func(*controlpb.RunnerFrame) error, func(*controlpb.RunnerFrame) error) ExecutorConnection
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
		writeTimeout:      controlWriteTimeout,
		openExecutor:      dependencies.OpenExecutor,
	}, nil
}

// Run keeps the authenticated control stream connected until ctx ends or a local operation fails.
func (c *Client) Run(ctx context.Context, endpoint Endpoint) error {
	return stream.Retry(ctx, "control", func(ctx context.Context) (bool, error) {
		return c.connect(ctx, endpoint)
	}, c.stopRetry)
}

func (c *Client) connect(ctx context.Context, endpoint Endpoint) (bool, error) {
	credentials, err := endpoint.TLS.ClientCredentials(runnertransport.GatewayURI)
	if err != nil {
		return false, err
	}
	conn, err := stream.NewSecureConnection(endpoint.Target, credentials)
	if err != nil {
		return false, err
	}
	defer conn.Close()
	ctx, cancel := context.WithCancel(ctx)
	var receiving, calls, adoption sync.WaitGroup
	var executor ExecutorConnection
	var outbound *sender
	defer func() {
		cancel()
		if executor != nil {
			executor.Close()
		}
		if outbound != nil {
			outbound.wait()
		}
		calls.Wait()
		receiving.Wait()
		adoption.Wait()
	}()
	controlStream, err := controlpb.NewRunnerControlClient(conn).Connect(ctx)
	if err != nil {
		return false, err
	}
	outbound = newSender(ctx, cancel, c.writeTimeout, controlStream.Send)
	send := outbound.Send
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
	if welcome == nil || welcome.IncarnationUid == "" || welcome.ConnectionEpoch == 0 {
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
			return streamStable(), outbound.failureOr(ctx.Err())
		case <-outbound.done:
			return streamStable(), outbound.error()
		case err := <-adopted:
			if err != nil {
				return false, err
			}
			if err := ctx.Err(); err != nil {
				return false, err
			}
			if c.openExecutor != nil {
				executor = c.openExecutor(ctx, welcome.ConnectionEpoch, send, outbound.SendExecutor)
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
			} else if frame := message.frame.GetExecutor(); frame != nil {
				if !incarnationAdopted {
					if frame.GetOpen() != nil {
						if err := send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Executor{Executor: &executorpb.ExecutorFrame{
							ChannelId: frame.GetChannelId(), Frame: &executorpb.ExecutorFrame_Ready{Ready: &executorpb.ExecutorReady{Accepted: false}},
						}}}); err != nil {
							return false, err
						}
					}
					continue
				}
				if executor == nil {
					return false, status.Error(codes.Unimplemented, "executor relay is not configured")
				}
				if err := executor.Route(frame); err != nil {
					return false, err
				}
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

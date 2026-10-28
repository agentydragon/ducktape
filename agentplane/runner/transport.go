package runner

import (
	"context"
	"errors"
	"strings"
	"sync"
	"time"

	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/observability"
	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	"github.com/agentydragon/ducktape/agentplane/runner/ingress"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner/stream"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/backoff"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	heartbeatInterval = 10 * time.Second
	streamLiveness    = 30 * time.Second
)

func connection(endpoint Endpoint) (*grpc.ClientConn, error) {
	return grpc.NewClient(endpoint.Target, observability.GRPCClientOption(), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithNoProxy(), grpc.WithConnectParams(grpc.ConnectParams{Backoff: backoff.Config{BaseDelay: 500 * time.Millisecond, Multiplier: 2, Jitter: 0.2, MaxDelay: 30 * time.Second}, MinConnectTimeout: 10 * time.Second}))
}

func authenticated(ctx context.Context, endpoint Endpoint) context.Context {
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+endpoint.Bearer)
}

func (r *Runner) Run(ctx context.Context, control, ingress Endpoint) error {
	if err := r.Startup(r.ctx); err != nil {
		return errors.Join(err, r.Close())
	}
	streamCtx, cancel := context.WithCancel(context.Background())
	var streams sync.WaitGroup
	streams.Add(2)
	go func() {
		defer streams.Done()
		if err := r.controlLoop(streamCtx, control); err != nil {
			r.fail(err)
		}
	}()
	go func() {
		defer streams.Done()
		if err := r.ingressLoop(streamCtx, ingress); err != nil {
			r.fail(err)
		}
	}()
	var failure error
	select {
	case <-ctx.Done():
	case failure = <-r.fatal:
	}
	closeErr := r.Close()
	cancel()
	streams.Wait()
	return errors.Join(failure, closeErr)
}

func (r *Runner) controlLoop(ctx context.Context, endpoint Endpoint) error {
	return stream.Retry(ctx, "control", func(ctx context.Context) (bool, error) { return r.connectControl(ctx, endpoint) }, func(err error) bool {
		var storage *journal.StorageError
		return errors.As(err, &storage)
	})
}

func (r *Runner) connectControl(ctx context.Context, endpoint Endpoint) (bool, error) {
	conn, err := connection(endpoint)
	if err != nil {
		return false, err
	}
	defer conn.Close()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := controlpb.NewRunnerControlClient(conn).Connect(authenticated(ctx, endpoint))
	if err != nil {
		return false, err
	}
	var sending sync.Mutex
	send := func(frame *controlpb.RunnerFrame) error {
		sending.Lock()
		defer sending.Unlock()
		return stream.Send(frame)
	}
	if err := send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Hello{Hello: &controlpb.Hello{RunnerBootId: r.bootID}}}); err != nil {
		return false, err
	}
	type incoming struct {
		frame *controlpb.ServiceFrame
		err   error
	}
	received := make(chan incoming, 1)
	go func() {
		for {
			frame, err := stream.Recv()
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
	liveness := time.NewTimer(streamLiveness)
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
	if err := r.adoptIncarnation(r.ctx, welcome.IncarnationUid); err != nil {
		return false, err
	}
	started := time.Now()
	lastReceived := time.Now()
	ticker := time.NewTicker(heartbeatInterval)
	defer ticker.Stop()
	answers := make(chan error, 16)
	var calls sync.WaitGroup
	defer func() { cancel(); calls.Wait() }()
	var published *controlpb.RunnerFacts
	for {
		changed := r.changesSince()
		facts, err := r.facts()
		if err != nil {
			return false, err
		}
		if !proto.Equal(facts, published) {
			if err := send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Heartbeat{Heartbeat: &controlpb.RunnerHeartbeat{Facts: facts}}}); err != nil {
				return false, err
			}
			published = facts
		}
		select {
		case <-ctx.Done():
			return false, ctx.Err()
		case err := <-answers:
			if err != nil {
				return time.Since(started) >= streamLiveness, err
			}
		case message := <-received:
			if message.err != nil {
				return time.Since(started) >= streamLiveness, message.err
			}
			lastReceived = time.Now()
			if call := message.frame.GetCall(); call != nil {
				calls.Add(1)
				go func() {
					defer calls.Done()
					reply := r.relay(ctx, call)
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
			if time.Since(lastReceived) > streamLiveness {
				return time.Since(started) >= streamLiveness, errors.New("no frame from Runner Gateway")
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
func (r *Runner) relay(ctx context.Context, call *tunnelpb.Call) *tunnelpb.Reply {
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
		response, err := method.Handler(r, ctx, func(value any) error { return proto.Unmarshal(call.Request, value.(proto.Message)) }, nil)
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

func (r *Runner) ingressLoop(ctx context.Context, endpoint Endpoint) error {
	ctx, cancel := context.WithCancel(ctx)
	conn, err := connection(endpoint)
	if err != nil {
		return err
	}
	defer conn.Close()
	client := ingress.NewClient(ingresspb.NewRunnerIngressClient(conn), r.store, func(id string, ack retained.Acknowledgement) error {
		if err := r.lifecycle.publishAck(id, ack); err != nil {
			return err
		}
		r.changed()
		return nil
	})
	authCtx := authenticated(ctx, endpoint)
	active := map[string]bool{}
	finished := make(chan string, 16)
	var pushes sync.WaitGroup
	defer func() { cancel(); pushes.Wait() }()
	for {
		changed := r.changesSince()
		for _, s := range r.sessions.inventory() {
			if active[s.id] {
				continue
			}
			ack, err := r.store.ReadAcknowledgement(s.id)
			if err != nil {
				return err
			}
			if ack != nil && ack.Sealed {
				continue
			}
			active[s.id] = true
			pushes.Add(1)
			go func(s *Session) {
				defer pushes.Done()
				err := client.Deliver(authCtx, ingress.Source{ID: s.id, Announce: s.attached, Journal: s.journal})
				if err != nil {
					r.fail(err)
				}
				select {
				case finished <- s.id:
				case <-ctx.Done():
				}
			}(s)
		}
		select {
		case <-ctx.Done():
			return nil
		case id := <-finished:
			delete(active, id)
		case <-changed:
		}
	}
}

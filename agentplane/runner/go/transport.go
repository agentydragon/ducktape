package runner

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"strings"
	"sync"
	"time"

	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	controlpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
	ingresspb "github.com/agentydragon/ducktape/agentplane/proto/runner_ingress"
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
	ingressInFlight   = 1024
)

type localStreamError struct{ err error }

func (e *localStreamError) Error() string { return e.err.Error() }
func (e *localStreamError) Unwrap() error { return e.err }

func connection(endpoint Endpoint) (*grpc.ClientConn, error) {
	return grpc.NewClient(endpoint.Target, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithNoProxy(), grpc.WithConnectParams(grpc.ConnectParams{Backoff: backoff.Config{BaseDelay: 500 * time.Millisecond, Multiplier: 2, Jitter: 0.2, MaxDelay: 30 * time.Second}, MinConnectTimeout: 10 * time.Second}))
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

func retry(ctx context.Context, name string, attempt func(context.Context) (bool, error)) error {
	delay := 500 * time.Millisecond
	for {
		progress, err := attempt(ctx)
		if ctx.Err() != nil {
			return nil
		}
		if err == nil {
			return nil
		}
		var storage *JournalStorageError
		if errors.As(err, &storage) {
			return err
		}
		var local *localStreamError
		if errors.As(err, &local) {
			return err
		}
		if progress {
			delay = 500 * time.Millisecond
		}
		wait := time.Duration(rand.Float64() * float64(delay))
		if code := status.Code(err); code == codes.PermissionDenied || code == codes.FailedPrecondition {
			wait = 30 * time.Second
		}
		slog.Warn("runner stream lost", "stream", name, "error", err, "retry_after", wait)
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil
		case <-timer.C:
		}
		if delay < 30*time.Second {
			delay *= 2
			if delay > 30*time.Second {
				delay = 30 * time.Second
			}
		}
	}
}

func (r *Runner) controlLoop(ctx context.Context, endpoint Endpoint) error {
	return retry(ctx, "control", func(ctx context.Context) (bool, error) { return r.connectControl(ctx, endpoint) })
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
	if call.Timeout != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, call.Timeout.AsDuration())
		defer cancel()
	}
	reply := &tunnelpb.Reply{CallId: call.CallId}
	name, ok := strings.CutPrefix(call.Method, "/"+controlpb.Runner_ServiceDesc.ServiceName+"/")
	if !ok {
		reply.Code = int32(codes.Unimplemented)
		reply.Details = "unknown Runner method"
		return reply
	}
	for _, method := range controlpb.Runner_ServiceDesc.Methods {
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
	stub := ingresspb.NewRunnerIngressClient(conn)
	active := map[string]bool{}
	finished := make(chan string, 16)
	var pushes sync.WaitGroup
	defer func() { cancel(); pushes.Wait() }()
	for {
		changed := r.changesSince()
		for _, s := range r.inventory() {
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
				err := retry(ctx, "ingress "+s.id, func(ctx context.Context) (bool, error) { return r.pushOnce(ctx, endpoint, stub, s) })
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

func (r *Runner) pushOnce(ctx context.Context, endpoint Endpoint, stub ingresspb.RunnerIngressClient, s *Session) (bool, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := stub.Push(authenticated(ctx, endpoint))
	if err != nil {
		return false, err
	}
	if err := stream.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Announce{Announce: s.attached()}}); err != nil {
		return false, err
	}
	type receivedAck struct {
		ack *ingresspb.Acknowledgement
		err error
	}
	received := make(chan receivedAck, 1)
	go func() {
		for {
			ack, err := stream.Recv()
			select {
			case received <- receivedAck{ack, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	timer := time.NewTimer(streamLiveness)
	defer timer.Stop()
	var first receivedAck
	select {
	case first = <-received:
	case <-timer.C:
		return false, errors.New("no first acknowledgement from ingress")
	case <-ctx.Done():
		return false, ctx.Err()
	}
	if first.err != nil {
		return false, first.err
	}
	if first.ack.Cursor > s.journal.LastCursor() {
		return false, status.Error(codes.FailedPrecondition, "ingress cursor exceeds the journal")
	}
	acknowledged, sent := first.ack.Cursor, first.ack.Cursor
	if err := r.store.WriteAcknowledgement(s.id, IngressAcknowledgement{Cursor: acknowledged, Sealed: first.ack.Sealed}); err != nil {
		return false, &localStreamError{err}
	}
	r.changed()
	if first.ack.Sealed {
		return true, nil
	}
	progressed, sealedSent := false, false
	lastReceived, lastSent := time.Now(), time.Now()
	ticker := time.NewTicker(heartbeatInterval)
	defer ticker.Stop()
	for {
		changed := r.changesSince()
		if !sealedSent && sent < acknowledged+ingressInFlight {
			limit := 128
			if remaining := acknowledged + ingressInFlight - sent; remaining < uint64(limit) {
				limit = int(remaining)
			}
			entries, err := s.journal.Since(ctx, sent, limit)
			if err != nil {
				return progressed, err
			}
			for _, entry := range entries {
				sent = entry.Cursor
				if err := stream.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Entry{Entry: entry}}); err != nil {
					return progressed, err
				}
				lastSent = time.Now()
			}
			if len(entries) > 0 {
				continue
			}
			if seal := s.journal.SealState(); seal != nil && sent == seal.Cursor {
				uid := seal.IncarnationUID
				if err := stream.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Sealed{Sealed: &runnerpb.Sealed{Cursor: seal.Cursor, IncarnationUid: &uid}}}); err != nil {
					return progressed, err
				}
				sealedSent = true
				if err := stream.CloseSend(); err != nil {
					return progressed, err
				}
			}
		}
		select {
		case <-ctx.Done():
			return progressed, ctx.Err()
		case message := <-received:
			if message.err != nil {
				if message.err == io.EOF {
					message.err = errors.New("ingress ended before acknowledging the seal")
				}
				return progressed, message.err
			}
			ack := message.ack
			lastReceived = time.Now()
			if ack.Cursor < acknowledged || ack.Cursor > sent {
				return progressed, status.Error(codes.FailedPrecondition, fmt.Sprintf("invalid ingress acknowledgement %d after %d, sent %d", ack.Cursor, acknowledged, sent))
			}
			progressed = progressed || ack.Cursor > acknowledged
			acknowledged = ack.Cursor
			if ack.Sealed {
				seal := s.journal.SealState()
				if !sealedSent || seal == nil || ack.Cursor != seal.Cursor {
					return progressed, status.Error(codes.FailedPrecondition, "ingress acknowledged an unsent seal")
				}
			}
			if err := r.store.WriteAcknowledgement(s.id, IngressAcknowledgement{Cursor: ack.Cursor, Sealed: ack.Sealed}); err != nil {
				return progressed, &localStreamError{err}
			}
			r.changed()
			if ack.Sealed {
				return true, nil
			}
		case <-ticker.C:
			if time.Since(lastReceived) > streamLiveness {
				return progressed, errors.New("no frame from ingress")
			}
			if !sealedSent && time.Since(lastSent) >= heartbeatInterval {
				if err := stream.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Heartbeat{Heartbeat: &ingresspb.Heartbeat{}}}); err != nil {
					return progressed, err
				}
				lastSent = time.Now()
			}
		case <-changed:
		}
	}
}

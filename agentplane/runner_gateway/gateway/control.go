package gateway

import (
	"context"
	"errors"
	"io"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type runnerControlServer struct {
	runnercontrolpb.UnimplementedRunnerControlServer
	gateway *Gateway
}

type runnerFrameResult struct {
	frame *runnercontrolpb.RunnerFrame
	err   error
}

// controlFrameSender serializes server writes and permanently stops after a failed or timed-out
// write. Returning from Connect then closes the gRPC stream, which unblocks an in-flight Send.
// Without the terminal state, a concurrent tunnel call could start a second Send while the first
// one was still blocked after its deadline.
type controlFrameSender struct {
	ctx     context.Context
	timeout time.Duration
	send    func(*runnercontrolpb.ServiceFrame) error

	mu     sync.Mutex
	failed error
}

func (s *controlFrameSender) Send(frame *runnercontrolpb.ServiceFrame) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failed != nil {
		return s.failed
	}
	err := streaming.SendWithin(s.ctx, s.timeout, s.send, frame)
	switch {
	case err == nil:
		return nil
	case errors.Is(err, streaming.ErrSendTimeout):
		s.failed = status.Error(codes.Unavailable, "runner control stream send timed out")
	case s.ctx.Err() != nil && errors.Is(err, s.ctx.Err()):
		s.failed = status.FromContextError(s.ctx.Err()).Err()
	default:
		s.failed = status.Error(codes.Unavailable, "runner control stream ended while sending")
	}
	return s.failed
}

func (s *runnerControlServer) Connect(stream grpc.BidiStreamingServer[runnercontrolpb.RunnerFrame, runnercontrolpb.ServiceFrame]) (returnErr error) {
	g := s.gateway
	timing := g.config.ControlTiming
	ctx := stream.Context()

	resolveCtx, cancel := context.WithTimeout(ctx, timing.LivenessTimeout)
	principal, err := g.runnerAuth.Resolve(resolveCtx)
	cancel()
	if err != nil {
		return err
	}
	resolveCtx, cancel = context.WithTimeout(ctx, timing.LivenessTimeout)
	identity, podUID, err := g.sandboxes.runnerPeer(resolveCtx, principal)
	cancel()
	if err != nil {
		return unavailable(err)
	}

	first, err := receiveFirstFrame(ctx, stream, timing.LivenessTimeout)
	if err != nil || first.GetHello() == nil || first.GetHello().GetRunnerBootId() == "" {
		return status.Error(codes.InvalidArgument, "the first frame must be Hello")
	}

	recordCtx, recordCancel := context.WithTimeout(ctx, timing.LivenessTimeout)
	epoch, err := g.records.accept(recordCtx, identity, podUID, first.GetHello().GetRunnerBootId())
	recordCancel()
	if err != nil {
		return unavailable(err)
	}

	sender := &controlFrameSender{ctx: ctx, timeout: timing.LivenessTimeout, send: stream.Send}
	send := sender.Send
	connection := newRunnerConnection(identity, podUID, first.GetHello().GetRunnerBootId(), epoch, send)
	// Publish the local tunnel before Welcome lets the runner start consuming calls.
	g.connections.accept(connection)
	defer func() {
		if returnErr == nil {
			returnErr = status.Error(codes.Unavailable, "runner control stream ended")
		}
		g.connections.remove(connection, returnErr)
	}()

	if err := send(&runnercontrolpb.ServiceFrame{Frame: &runnercontrolpb.ServiceFrame_Welcome{
		Welcome: &runnercontrolpb.Welcome{ConnectionEpoch: epoch, IncarnationUid: podUID},
	}}); err != nil {
		return err
	}

	frames := make(chan runnerFrameResult, 1)
	go func() {
		for {
			frame, err := stream.Recv()
			select {
			case frames <- runnerFrameResult{frame: frame, err: err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()

	started := time.Now()
	lastFrame := started
	lastTouch := started
	lastFrameAt := started.UTC()
	var facts *sandboxlease.RunnerFacts
	urgent := false
	heartbeats := time.NewTicker(timing.HeartbeatInterval)
	defer heartbeats.Stop()
	for {
		sendHeartbeat := false
		select {
		case result := <-frames:
			if result.err != nil {
				if result.err == io.EOF || status.Code(result.err) == codes.Canceled || status.Code(result.err) == codes.Unknown {
					return status.Error(codes.Unavailable, "runner control stream ended")
				}
				return result.err
			}
			lastFrame = time.Now()
			lastFrameAt = lastFrame.UTC()
			switch frame := result.frame.GetFrame().(type) {
			case *runnercontrolpb.RunnerFrame_Heartbeat:
				if frame.Heartbeat.GetFacts() != nil {
					nextValue, err := sandboxlease.FromProto(frame.Heartbeat.GetFacts())
					if err != nil {
						return status.Error(codes.InvalidArgument, "runner sent invalid facts")
					}
					next := &nextValue
					if facts == nil || facts.Stage != next.Stage || facts.Bootstrap != next.Bootstrap || facts.SetupsRunning != next.SetupsRunning {
						urgent = true
					}
					facts = next
				}
			case *runnercontrolpb.RunnerFrame_Reply:
				connection.tunnel.answer(frame.Reply)
			default:
				return status.Error(codes.InvalidArgument, "unexpected runner control frame")
			}
		case <-heartbeats.C:
			sendHeartbeat = true
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-connection.ended:
			return connection.error()
		}

		now := time.Now()
		if now.Sub(lastFrame) > timing.LivenessTimeout {
			return status.Error(codes.Unavailable, "no frame from the runner")
		}
		if now.Sub(started) > timing.MaximumAge {
			return status.Error(codes.Unavailable, "maximum connection age reached")
		}
		if urgent || now.Sub(lastTouch) >= timing.LeaseTouchInterval {
			touchCtx, touchCancel := context.WithTimeout(ctx, timing.LivenessTimeout)
			err := g.records.renew(touchCtx, identity, epoch, lastFrameAt, facts)
			if err == nil {
				var current sandboxreader.Identity
				current, currentPodUID, resolveErr := g.sandboxes.runnerPeer(touchCtx, principal)
				if resolveErr != nil || current != identity || currentPodUID != podUID {
					err = status.Error(codes.PermissionDenied, "the Sandbox routing changed")
				}
			}
			touchCancel()
			if err != nil {
				if status.Code(err) == codes.Aborted || status.Code(err) == codes.PermissionDenied {
					return err
				}
				return unavailable(err)
			}
			lastTouch = now
			urgent = false
			sendHeartbeat = true
		}
		if sendHeartbeat {
			if err := send(&runnercontrolpb.ServiceFrame{Frame: &runnercontrolpb.ServiceFrame_Heartbeat{
				Heartbeat: &runnercontrolpb.Heartbeat{},
			}}); err != nil {
				return err
			}
		}
	}
}

func receiveFirstFrame(
	ctx context.Context,
	stream grpc.BidiStreamingServer[runnercontrolpb.RunnerFrame, runnercontrolpb.ServiceFrame],
	timeout time.Duration,
) (*runnercontrolpb.RunnerFrame, error) {
	result := make(chan runnerFrameResult, 1)
	go func() {
		frame, err := stream.Recv()
		result <- runnerFrameResult{frame: frame, err: err}
	}()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case first := <-result:
		return first.frame, first.err
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-timer.C:
		return nil, context.DeadlineExceeded
	}
}

var _ runnercontrolpb.RunnerControlServer = (*runnerControlServer)(nil)

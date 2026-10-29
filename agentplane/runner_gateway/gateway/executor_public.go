package gateway

import (
	"context"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type executorGatewayServer struct {
	executorpb.UnimplementedExecutorGatewayServer
	gateway *Gateway
}

type executorRequestResult struct {
	frame *executorpb.ExecutorFrame
	err   error
}

func (s *executorGatewayServer) Connect(stream grpc.BidiStreamingServer[executorpb.ExecutorFrame, executorpb.ExecutorFrame]) error {
	g := s.gateway
	ctx := stream.Context()
	if err := g.authorize(ctx, g.executorCallers); err != nil {
		return err
	}

	first, err := receiveFirstExecutorFrame(ctx, stream, g.config.ControlTiming.LivenessTimeout)
	if err != nil || first.GetOpen() == nil {
		return status.Error(codes.InvalidArgument, "the first frame must be an ExecutorOpen")
	}
	open := first.GetOpen()
	if open.GetDestination() == nil || open.GetExpectedIncarnationUid() == "" || open.GetSessionId() == "" || len(open.GetSessionId()) > 128 {
		return status.Error(codes.InvalidArgument, "destination, expected incarnation and Session ID are required")
	}

	resolveCtx, cancel := context.WithTimeout(ctx, g.config.ControlTiming.LivenessTimeout)
	view, err := g.sandboxes.destination(resolveCtx, open.GetDestination(), true)
	cancel()
	if err != nil {
		return unavailable(err)
	}
	if view.incarnation == nil || open.GetExpectedIncarnationUid() != view.incarnation.UID {
		return status.Error(codes.FailedPrecondition, "Sandbox incarnation changed")
	}
	connection, err := g.connections.holder(view.identity.UID)
	if err != nil {
		return err
	}
	if connection.identity != view.identity || connection.podUID != open.GetExpectedIncarnationUid() {
		return status.Error(codes.FailedPrecondition, "Sandbox incarnation changed")
	}
	return s.relayExecutor(stream, connection, open)
}

func receiveFirstExecutorFrame(
	ctx context.Context,
	stream grpc.BidiStreamingServer[executorpb.ExecutorFrame, executorpb.ExecutorFrame],
	timeout time.Duration,
) (*executorpb.ExecutorFrame, error) {
	result := make(chan executorRequestResult, 1)
	go func() {
		frame, err := stream.Recv()
		result <- executorRequestResult{frame: frame, err: err}
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

var _ executorpb.ExecutorGatewayServer = (*executorGatewayServer)(nil)

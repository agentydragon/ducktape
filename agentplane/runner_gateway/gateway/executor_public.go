package gateway

import (
	"context"
	"io"
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
	if err != nil || first.GetOpen() == nil || first.GetChannelId() != "" {
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
	if connection.direct != nil {
		return s.forwardExecutor(stream, connection, open)
	}
	channel, err := connection.executor.open(open)
	if err != nil {
		return err
	}
	defer func() {
		_ = connection.closeExecutor(channel.id, &executorpb.ExecutorClose{Code: 1000})
	}()

	readyTimer := time.NewTimer(g.config.ControlTiming.LivenessTimeout)
	defer readyTimer.Stop()
	if err := channel.waitReady(ctx, readyTimer.C); err != nil {
		return err
	}
	if err := sendExecutorPublicFrame(ctx, stream, g.config.ControlTiming.LivenessTimeout,
		&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Ready{Ready: &executorpb.ExecutorReady{Accepted: true}}}); err != nil {
		return err
	}

	requests := make(chan executorRequestResult, 1)
	go func() {
		for {
			frame, err := stream.Recv()
			select {
			case requests <- executorRequestResult{frame: frame, err: err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()

	for {
		// Drain already queued executor messages before observing the terminal close/error state.
		select {
		case frame := <-channel.frames:
			if err := sendExecutorPublicFrame(ctx, stream, g.config.ControlTiming.LivenessTimeout, publicExecutorFrame(frame)); err != nil {
				return err
			}
			continue
		default:
		}
		select {
		case frame := <-channel.frames:
			if err := sendExecutorPublicFrame(ctx, stream, g.config.ControlTiming.LivenessTimeout, publicExecutorFrame(frame)); err != nil {
				return err
			}
		case <-channel.done:
			err, closeFrame := channel.end()
			if err != nil {
				return err
			}
			if closeFrame != nil {
				return sendExecutorPublicFrame(ctx, stream, g.config.ControlTiming.LivenessTimeout,
					&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Close{Close: closeFrame}})
			}
			return nil
		case request := <-requests:
			if request.err != nil {
				if request.err == io.EOF {
					return connection.closeExecutor(channel.id, &executorpb.ExecutorClose{Code: 1000})
				}
				return request.err
			}
			if request.frame.GetChannelId() != "" {
				return status.Error(codes.InvalidArgument, "public executor frames cannot set a channel ID")
			}
			switch body := request.frame.GetFrame().(type) {
			case *executorpb.ExecutorFrame_Message:
				if body.Message.GetOpcode() != executorpb.ExecutorMessage_OPCODE_TEXT && body.Message.GetOpcode() != executorpb.ExecutorMessage_OPCODE_BINARY {
					return status.Error(codes.InvalidArgument, "unknown executor message opcode")
				}
				if len(body.Message.GetPayload()) > maxExecutorMessageBytes {
					return status.Error(codes.ResourceExhausted, "executor message exceeds the size limit")
				}
				if err := connection.sendExecutorMessage(ctx, channel.id, body.Message); err != nil {
					return err
				}
			case *executorpb.ExecutorFrame_Close:
				if err := connection.closeExecutor(channel.id, body.Close); err != nil {
					return err
				}
				return sendExecutorPublicFrame(ctx, stream, g.config.ControlTiming.LivenessTimeout,
					&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Close{Close: body.Close}})
			default:
				return status.Error(codes.InvalidArgument, "only executor messages and close frames follow ExecutorOpen")
			}
		case <-connection.ended:
			return connection.error()
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		}
	}
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

func sendExecutorPublicFrame(
	ctx context.Context,
	stream grpc.BidiStreamingServer[executorpb.ExecutorFrame, executorpb.ExecutorFrame],
	timeout time.Duration,
	frame *executorpb.ExecutorFrame,
) error {
	result := make(chan error, 1)
	go func() { result <- stream.Send(frame) }()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case err := <-result:
		return err
	case <-ctx.Done():
		return status.FromContextError(ctx.Err()).Err()
	case <-timer.C:
		return status.Error(codes.Unavailable, "executor Gateway stream send timed out")
	}
}

func publicExecutorFrame(frame *executorpb.ExecutorFrame) *executorpb.ExecutorFrame {
	switch body := frame.GetFrame().(type) {
	case *executorpb.ExecutorFrame_Message:
		return &executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Message{Message: body.Message}}
	case *executorpb.ExecutorFrame_Close:
		return &executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Close{Close: body.Close}}
	default:
		return &executorpb.ExecutorFrame{}
	}
}

var _ executorpb.ExecutorGatewayServer = (*executorGatewayServer)(nil)

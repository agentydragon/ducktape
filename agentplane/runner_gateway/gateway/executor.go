package gateway

import (
	"context"
	"io"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	runnerexecpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const maxExecutorMessageBytes = 1 << 20

func (s *executorGatewayServer) relayExecutor(public grpc.BidiStreamingServer[executorpb.ExecutorFrame, executorpb.ExecutorFrame], connection *runnerConnection, open *executorpb.ExecutorOpen) error {
	ctx, cancel := context.WithCancel(public.Context())
	defer cancel()
	go func() {
		select {
		case <-connection.ended:
			cancel()
		case <-ctx.Done():
		}
	}()
	private, err := runnerexecpb.NewRunnerExecClient(connection.direct).Connect(ctx)
	if err != nil {
		return err
	}
	timeout := s.gateway.config.ControlTiming.LivenessTimeout
	if err := streaming.SendWithin(ctx, timeout, private.Send, &executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Open{Open: open}}); err != nil {
		return err
	}
	fromPublic := make(chan executorRequestResult, 1)
	fromPrivate := make(chan executorRequestResult, 1)
	go receiveExecutorFrames(ctx, public.Recv, fromPublic)
	go receiveExecutorFrames(ctx, private.Recv, fromPrivate)
	readyTimer := time.NewTimer(timeout)
	defer readyTimer.Stop()
	readyDeadline := readyTimer.C
	var publicFrames <-chan executorRequestResult
	ready := false
	for {
		select {
		case <-readyDeadline:
			return status.Error(codes.DeadlineExceeded, "Sandbox executor did not open in time")
		case response := <-fromPrivate:
			if response.err != nil {
				if response.err == io.EOF {
					return nil
				}
				return response.err
			}
			frame := response.frame
			if err := validateExecutorFrame(frame); err != nil {
				return err
			}
			if !ready {
				if frame.GetReady() == nil {
					return status.Error(codes.InvalidArgument, "executor sent data before readiness")
				}
				if !frame.GetReady().GetAccepted() {
					return status.Error(codes.Unavailable, "Sandbox executor is unavailable")
				}
				ready = true
				readyTimer.Stop()
				readyDeadline = nil
				publicFrames = fromPublic
			} else if frame.GetMessage() == nil && frame.GetClose() == nil {
				return status.Error(codes.InvalidArgument, "executor repeated readiness or sent an invalid frame")
			}
			if err := streaming.SendWithin(ctx, timeout, public.Send, frame); err != nil {
				return err
			}
			if frame.GetClose() != nil {
				return nil
			}
		case request := <-publicFrames:
			if request.err != nil {
				if request.err == io.EOF {
					return streaming.SendWithin(ctx, timeout, private.Send, &executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Close{Close: &executorpb.ExecutorClose{Code: 1000}}})
				}
				return request.err
			}
			frame := request.frame
			if err := validateExecutorFrame(frame); err != nil {
				return err
			}
			if frame.GetMessage() == nil && frame.GetClose() == nil {
				return status.Error(codes.InvalidArgument, "only executor messages and close frames follow ExecutorOpen")
			}
			if err := streaming.SendWithin(ctx, timeout, private.Send, frame); err != nil {
				return err
			}
			if frame.GetClose() != nil {
				return streaming.SendWithin(ctx, timeout, public.Send, frame)
			}
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		}
	}
}

func receiveExecutorFrames(ctx context.Context, receive func() (*executorpb.ExecutorFrame, error), responses chan<- executorRequestResult) {
	for {
		frame, err := receive()
		select {
		case responses <- executorRequestResult{frame, err}:
		case <-ctx.Done():
			return
		}
		if err != nil {
			return
		}
	}
}

func validateExecutorFrame(frame *executorpb.ExecutorFrame) error {
	if message := frame.GetMessage(); message != nil {
		if len(message.GetPayload()) > maxExecutorMessageBytes {
			return status.Error(codes.ResourceExhausted, "executor message exceeds the size limit")
		}
		if message.GetOpcode() != executorpb.ExecutorMessage_OPCODE_TEXT && message.GetOpcode() != executorpb.ExecutorMessage_OPCODE_BINARY {
			return status.Error(codes.InvalidArgument, "unknown executor message opcode")
		}
	}
	return nil
}

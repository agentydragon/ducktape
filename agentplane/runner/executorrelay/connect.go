package executorrelay

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	forwardWriteTimeout = 10 * time.Second
	forwardOpenTimeout  = 10 * time.Second
)

type directExecFrame struct {
	frame *executorpb.ExecutorFrame
	done  chan error
}

func enqueueDirectExecutorFrame(ctx context.Context, queue chan<- directExecFrame, item directExecFrame) error {
	select {
	case queue <- item:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (m *Manager) Connect(
	server grpc.BidiStreamingServer[executorpb.ExecutorFrame, executorpb.ExecutorFrame],
) (result error) {
	epoch, err := m.config.Authority.Authorize(server.Context())
	if err != nil {
		return err
	}
	if err := m.config.Authority.Validate(epoch); err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(server.Context())
	stopWatch := context.AfterFunc(epoch.Context(), cancel)
	defer func() {
		stopWatch()
		cancel()
	}()
	sendQueue := make(chan directExecFrame, executorQueueDepth)
	sendErrors := make(chan error, 1)
	terminal := make(chan struct{})
	var terminalOnce sync.Once
	reportSendError := func(err error) {
		select {
		case sendErrors <- err:
		default:
		}
	}
	queueFrame := func(frame *executorpb.ExecutorFrame) error {
		item := directExecFrame{frame: frame}
		endsStream := frame.GetClose() != nil || frame.GetReady() != nil && !frame.GetReady().GetAccepted()
		if endsStream {
			item.done = make(chan error, 1)
		}
		if err := enqueueDirectExecutorFrame(ctx, sendQueue, item); err != nil {
			return err
		}
		if endsStream {
			select {
			case err := <-item.done:
				if err != nil {
					return err
				}
			case <-ctx.Done():
				return ctx.Err()
			}
			terminalOnce.Do(func() { close(terminal) })
		}
		return nil
	}
	toExecutor := queueFrame
	executor, err := m.Open(ctx, epoch.Number(), toExecutor, toExecutor)
	if err != nil {
		return err
	}
	defer func() {
		cancel()
		executor.Close()
	}()
	if err := server.SendHeader(metadata.MD{}); err != nil {
		return err
	}
	sessionID := ""
	defer func() {
		if result != nil && !errors.Is(result, io.EOF) && !errors.Is(result, context.Canceled) && status.Code(result) != codes.Canceled {
			slog.Warn("Runner executor stream ended", "session_id", sessionID, "error", result)
		}
	}()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case item := <-sendQueue:
				err := streaming.SendWithin(ctx, forwardWriteTimeout, server.Send, item.frame)
				if item.done != nil {
					item.done <- err
				}
				if err != nil {
					if errors.Is(err, streaming.ErrSendTimeout) {
						reportSendError(status.Error(codes.DeadlineExceeded, "Runner executor write timed out"))
					} else {
						reportSendError(err)
					}
					return
				}
			}
		}
	}()
	type incoming struct {
		frame *executorpb.ExecutorFrame
		err   error
	}
	received := make(chan incoming, 1)
	go func() {
		for {
			frame, err := server.Recv()
			select {
			case received <- incoming{frame: frame, err: err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	opened := false
	openTimer := time.NewTimer(forwardOpenTimeout)
	defer openTimer.Stop()
	for {
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-terminal:
			return nil
		case <-openTimer.C:
			if !opened {
				return status.Error(codes.DeadlineExceeded, "Runner executor stream did not open a channel")
			}
		case err := <-sendErrors:
			return err
		case message := <-received:
			if message.err != nil {
				return message.err
			}
			if err := m.config.Authority.Validate(epoch); err != nil {
				return err
			}
			frame := message.frame
			if frame == nil || frame.GetFrame() == nil {
				return status.Error(codes.InvalidArgument, "direct executor frames must contain a frame")
			}
			if !opened {
				if frame.GetOpen() == nil {
					return status.Error(codes.InvalidArgument, "direct executor stream must start with ExecutorOpen")
				}
				sessionID = frame.GetOpen().GetSessionId()
				opened = true
				openTimer.Stop()
			} else if frame.GetOpen() != nil || (frame.GetMessage() == nil && frame.GetClose() == nil) {
				return status.Error(codes.InvalidArgument, "only executor messages and close frames follow ExecutorOpen")
			}
			if message := frame.GetMessage(); message != nil {
				if len(message.GetPayload()) > maxExecutorMessageBytes {
					return status.Error(codes.ResourceExhausted, "executor message exceeds the size limit")
				}
				if message.GetOpcode() != executorpb.ExecutorMessage_OPCODE_TEXT && message.GetOpcode() != executorpb.ExecutorMessage_OPCODE_BINARY {
					return status.Error(codes.InvalidArgument, "unknown executor message opcode")
				}
			}
			if err := executor.Route(frame); err != nil {
				return err
			}
			if frame.GetClose() != nil {
				return nil
			}
		}
	}
}

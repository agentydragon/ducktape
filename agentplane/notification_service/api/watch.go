package api

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// sendWithin writes within the timeout: a caller that stops reading would otherwise hold the stream open indefinitely.
func sendWithin[Frame any](ctx context.Context, stream grpc.ServerStreamingServer[Frame], timeout time.Duration, frame *Frame) error {
	err := streaming.SendWithin(ctx, timeout, stream.Send, frame)
	switch {
	case errors.Is(err, streaming.ErrSendTimeout):
		return status.Error(codes.DeadlineExceeded, "notification watch write deadline expired")
	case errors.Is(err, context.Canceled):
		return status.FromContextError(context.Canceled).Err()
	case errors.Is(err, context.DeadlineExceeded):
		return status.FromContextError(context.DeadlineExceeded).Err()
	default:
		return err
	}
}

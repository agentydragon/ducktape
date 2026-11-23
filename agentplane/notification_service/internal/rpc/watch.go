package rpc

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"google.golang.org/grpc"
)

// sendWithin writes within the timeout: a caller that stops reading would otherwise hold the stream open indefinitely.
func sendWithin[Frame any](ctx context.Context, stream grpc.ServerStreamingServer[Frame], timeout time.Duration, frame *Frame) error {
	return streaming.SendWithinStatus(ctx, timeout, stream.Send, frame, "notification watch write deadline expired")
}

// Package graceful stops a gRPC server served on its own listener.
package graceful

import (
	"context"

	"google.golang.org/grpc"
)

// Stop stops server accepting connections and calls, and waits for the calls in flight to finish until ctx ends;
// then it closes those still open. It returns once server has stopped.
func Stop(ctx context.Context, server *grpc.Server) {
	stopped := make(chan struct{})
	go func() { server.GracefulStop(); close(stopped) }()
	select {
	case <-stopped:
	case <-ctx.Done():
		server.Stop()
		<-stopped
	}
}

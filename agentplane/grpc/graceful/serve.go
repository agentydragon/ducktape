package graceful

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"time"

	"google.golang.org/grpc"
)

// Grace bounds how long open calls may finish once a service is told to stop.
const Grace = 5 * time.Second

// GRPC is a gRPC server and the listener it serves; Name labels the listener's failure.
type GRPC struct {
	Name     string
	Server   *grpc.Server
	Listener net.Listener
}

// HTTP is an HTTP server and the listener it serves; Name labels the listener's failure.
type HTTP struct {
	Name     string
	Server   *http.Server
	Listener net.Listener
}

// Group is what a service serves until it stops.
type Group struct {
	GRPC []GRPC
	HTTP []HTTP
	// Failures carries other failures that stop the service, such as its background workers'; nil when none.
	Failures <-chan error
	// Drain runs once serving should end, before any server stops: it marks health NOT_SERVING and ends long calls.
	Drain func()
}

// Serve serves g until a listener or Failures fails or ctx ends. It then drains, shuts the HTTP servers down and
// stops the gRPC servers in order, each within its own Grace, and returns the first failure or ctx's error.
func Serve(ctx context.Context, g Group) error {
	failed := make(chan error, len(g.GRPC)+len(g.HTTP))
	for _, s := range g.GRPC {
		go func() { failed <- listenerFailure(s.Name, s.Server.Serve(s.Listener)) }()
	}
	for _, s := range g.HTTP {
		go func() { failed <- listenerFailure(s.Name, s.Server.Serve(s.Listener)) }()
	}
	var result error
	select {
	case result = <-failed:
	case result = <-g.Failures:
	case <-ctx.Done():
		result = ctx.Err()
	}
	g.Drain()
	stopping := context.WithoutCancel(ctx)
	for _, s := range g.HTTP {
		within(stopping, func(ctx context.Context) {
			if err := s.Server.Shutdown(ctx); err != nil {
				s.Server.Close()
			}
		})
	}
	for _, s := range g.GRPC {
		within(stopping, func(ctx context.Context) { Stop(ctx, s.Server) })
	}
	return result
}

// listenerFailure labels a listener that stopped serving; a gRPC server stopped elsewhere returns nil.
func listenerFailure(name string, err error) error {
	if err == nil {
		err = errors.New("stopped unexpectedly")
	}
	return fmt.Errorf("%s listener: %w", name, err)
}

// within runs stop under its own Grace, so a slow server cannot spend the next one's.
func within(ctx context.Context, stop func(context.Context)) {
	ctx, cancel := context.WithTimeout(ctx, Grace)
	defer cancel()
	stop(ctx)
}

package graceful

import (
	"context"
	"fmt"
	"net"
	"net/http"

	"github.com/soheilhy/cmux"
	"google.golang.org/grpc"
)

// Shared serves a gRPC server and an HTTP server on one listener, each through its own native Serve: a connection
// that opens with the HTTP/2 client preface goes to the gRPC server, every other one (HTTP/1.x) to the HTTP server.
// Prior-knowledge HTTP/2 that is not gRPC is therefore not served.
type Shared struct {
	mux          cmux.CMux
	grpc         *grpc.Server
	http         *http.Server
	grpcListener net.Listener
	httpListener net.Listener
}

// NewShared takes over listener.
func NewShared(listener net.Listener, grpcServer *grpc.Server, httpServer *http.Server) *Shared {
	mux := cmux.New(listener)
	return &Shared{
		mux: mux, grpc: grpcServer, http: httpServer,
		grpcListener: mux.Match(cmux.HTTP2()), httpListener: mux.Match(cmux.Any()),
	}
}

// Serve answers until Shutdown, sending the end of the gRPC server, the HTTP server and the listener to failed, which
// must have room for three. An end that Shutdown causes is sent too.
func (s *Shared) Serve(failed chan<- error) {
	go func() { failed <- fmt.Errorf("gRPC server: %w", s.grpc.Serve(s.grpcListener)) }()
	go func() { failed <- fmt.Errorf("HTTP server: %w", s.http.Serve(s.httpListener)) }()
	go func() { failed <- fmt.Errorf("listener: %w", s.mux.Serve()) }()
}

// Shutdown closes the listener, lets the HTTP requests and then the gRPC calls in flight finish until ctx ends, and
// closes those left. The HTTP server stops first because a gRPC-Gateway facade's requests are gRPC calls.
func (s *Shared) Shutdown(ctx context.Context) {
	// Closing the HTTP server closes the shared listener under it.
	if err := s.http.Shutdown(ctx); err != nil {
		_ = s.http.Close()
	}
	Stop(ctx, s.grpc)
	s.mux.Close()
}

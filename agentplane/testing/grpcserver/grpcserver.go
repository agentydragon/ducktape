// Package grpcserver owns the loopback listener and serving lifetime of a test server.
package grpcserver

import (
	"errors"
	"net"
	"testing"

	"google.golang.org/grpc"
)

// Serve starts an already configured and registered server. Test cleanup stops it,
// closes the listener and joins Serve; caller cleanups registered later run first.
func Serve(t testing.TB, server *grpc.Server) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		if err := <-done; err != nil && !errors.Is(err, grpc.ErrServerStopped) {
			t.Error(err)
		}
	})
	return listener.Addr().String()
}

// ServeRegistered configures and serves a plain loopback test peer.
func ServeRegistered(t testing.TB, register func(*grpc.Server)) string {
	t.Helper()
	server := grpc.NewServer()
	register(server)
	return Serve(t, server)
}

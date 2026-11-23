// Package grpcserver owns listeners and connections for test gRPC servers.
package grpcserver

import (
	"context"
	"errors"
	"net"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

// Serve starts an already configured and registered server. Test cleanup stops it,
// closes the listener and joins Serve; caller cleanups registered later run first.
func Serve(t testing.TB, server *grpc.Server) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	serveOn(t, server, listener)
	return listener.Addr().String()
}

func serveOn(t testing.TB, server *grpc.Server, listener net.Listener) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		if err := <-done; err != nil && !errors.Is(err, grpc.ErrServerStopped) {
			t.Error(err)
		}
	})
}

// ServeRegistered configures and serves a plain loopback test peer.
func ServeRegistered(t testing.TB, register func(*grpc.Server)) string {
	t.Helper()
	server := grpc.NewServer()
	register(server)
	return Serve(t, server)
}

// ConnectInMemory serves a configured server and returns a plaintext connection.
// Test cleanup closes the connection before stopping the server.
func ConnectInMemory(t testing.TB, server *grpc.Server, options ...grpc.DialOption) *grpc.ClientConn {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	serveOn(t, server, listener)
	options = append([]grpc.DialOption{
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }),
	}, options...)
	conn, err := grpc.NewClient("passthrough:///bufconn", options...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

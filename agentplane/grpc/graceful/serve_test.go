package graceful_test

import (
	"context"
	"errors"
	"net"
	"net/http"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"google.golang.org/grpc"
)

func listen(t *testing.T) net.Listener {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return listener
}

func TestServeStopsEveryServerWhenContextEnds(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	grpcServer := grpc.NewServer()
	httpServer := &http.Server{Handler: http.NotFoundHandler()}
	drained := false
	cancel()
	err := graceful.Serve(ctx, graceful.Group{
		GRPC:  []graceful.GRPC{{Name: "fixture gRPC", Server: grpcServer, Listener: listen(t)}},
		HTTP:  []graceful.HTTP{{Name: "fixture HTTP", Server: httpServer, Listener: listen(t)}},
		Drain: func() { drained = true },
	})
	if !errors.Is(err, context.Canceled) || !drained {
		t.Fatalf("Serve = %v, drained=%v; want context.Canceled after draining", err, drained)
	}
	// Both servers are stopped: neither serves a new listener.
	if err := grpcServer.Serve(listen(t)); !errors.Is(err, grpc.ErrServerStopped) {
		t.Errorf("gRPC Serve after stop = %v, want ErrServerStopped", err)
	}
	if err := httpServer.Serve(listen(t)); !errors.Is(err, http.ErrServerClosed) {
		t.Errorf("HTTP Serve after stop = %v, want ErrServerClosed", err)
	}
}

func TestServeReturnsTheFirstListenerFailure(t *testing.T) {
	broken := listen(t)
	broken.Close()
	err := graceful.Serve(t.Context(), graceful.Group{
		GRPC:  []graceful.GRPC{{Name: "fixture gRPC", Server: grpc.NewServer(), Listener: listen(t)}},
		HTTP:  []graceful.HTTP{{Name: "fixture HTTP", Server: &http.Server{Handler: http.NotFoundHandler()}, Listener: broken}},
		Drain: func() {},
	})
	if !errors.Is(err, net.ErrClosed) || !strings.HasPrefix(err.Error(), "fixture HTTP listener: ") {
		t.Fatalf("Serve = %v, want the HTTP listener's net.ErrClosed under its name", err)
	}
}

func TestServeReturnsAnotherFailure(t *testing.T) {
	workerFailed := errors.New("fixture worker failure")
	failures := make(chan error, 1)
	failures <- workerFailed
	err := graceful.Serve(t.Context(), graceful.Group{
		GRPC:     []graceful.GRPC{{Name: "fixture gRPC", Server: grpc.NewServer(), Listener: listen(t)}},
		Failures: failures,
		Drain:    func() {},
	})
	if !errors.Is(err, workerFailed) {
		t.Fatalf("Serve = %v, want %v", err, workerFailed)
	}
}

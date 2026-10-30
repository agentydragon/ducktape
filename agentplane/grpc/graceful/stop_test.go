package graceful_test

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

const waitLimit = 10 * time.Second

// blockingHealth answers Check once released, or fails it when the server ends the call.
type blockingHealth struct {
	healthpb.UnimplementedHealthServer
	entered chan struct{}
	release chan struct{}
}

func (h *blockingHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	close(h.entered)
	select {
	case <-h.release:
		return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
	case <-ctx.Done():
		return nil, status.FromContextError(ctx.Err()).Err()
	}
}

type call struct {
	response *healthpb.HealthCheckResponse
	err      error
}

// closeSignalingListener reports its first Close on closed.
type closeSignalingListener struct {
	net.Listener
	once   sync.Once
	closed chan struct{}
}

func (l *closeSignalingListener) Close() error {
	l.once.Do(func() { close(l.closed) })
	return l.Listener.Close()
}

// stopping serves a blocking Check, starts one call and returns once the handler holds it and Stop(ctx) has closed
// the listener; the channels carry the call's result, Stop's return and Serve's result, which grpc-go sends only once
// the server has stopped.
func stopping(t *testing.T, ctx context.Context) (*blockingHealth, <-chan call, <-chan struct{}, <-chan error) {
	t.Helper()
	tcp, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	listener := &closeSignalingListener{Listener: tcp, closed: make(chan struct{})}
	health := &blockingHealth{entered: make(chan struct{}), release: make(chan struct{})}
	server := grpc.NewServer()
	healthpb.RegisterHealthServer(server, health)
	served := make(chan error, 1)
	go func() { served <- server.Serve(listener) }()
	t.Cleanup(server.Stop)

	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	result := make(chan call, 1)
	go func() {
		response, err := healthpb.NewHealthClient(connection).Check(t.Context(), &healthpb.HealthCheckRequest{})
		result <- call{response, err}
	}()
	select {
	case <-health.entered:
	case <-time.After(waitLimit):
		t.Fatal("the call never reached its handler")
	}

	stopped := make(chan struct{})
	go func() { graceful.Stop(ctx, server); close(stopped) }()
	select {
	case <-listener.closed:
	case <-time.After(waitLimit):
		t.Fatal("stopping did not close the listener")
	}
	return health, result, stopped, served
}

func waitServed(t *testing.T, served <-chan error) {
	t.Helper()
	select {
	case err := <-served:
		if err != nil {
			t.Fatalf("Serve() = %v, want nil once stopped", err)
		}
	case <-time.After(waitLimit):
		t.Fatal("Serve did not return once the server stopped")
	}
}

func TestStopLetsTheCallInFlightFinish(t *testing.T) {
	health, result, stopped, served := stopping(t, t.Context())
	select {
	case <-stopped:
		t.Fatal("Stop returned with a call in flight")
	default:
	}

	close(health.release)
	select {
	case got := <-result:
		if got.err != nil || got.response.GetStatus() != healthpb.HealthCheckResponse_SERVING {
			t.Fatalf("the drained call = %v, %v; want its answer", got.response, got.err)
		}
	case <-time.After(waitLimit):
		t.Fatal("the drained call did not finish")
	}
	select {
	case <-stopped:
	case <-time.After(waitLimit):
		t.Fatal("Stop did not return once the call finished")
	}
	waitServed(t, served)
}

func TestStopEndsTheCallsLeftWhenItsContextEnds(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	_, result, stopped, served := stopping(t, ctx)
	cancel()
	select {
	case <-stopped:
	case <-time.After(waitLimit):
		t.Fatal("Stop did not return once its context ended")
	}
	select {
	case got := <-result:
		if got.err == nil {
			t.Fatalf("the call left open = %v, want it ended", got.response)
		}
	case <-time.After(waitLimit):
		t.Fatal("the call left open did not end")
	}
	waitServed(t, served)
}

package graceful_test

import (
	"context"
	"io"
	"net"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

type response struct {
	body string
	err  error
}

// One listener answers gRPC and HTTP, and shutting it down lets a call and a request in flight on it finish.
func TestSharedListenerServesBothAndDrainsBoth(t *testing.T) {
	tcp, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	listener := &closeSignalingListener{Listener: tcp, closed: make(chan struct{})}
	health := &blockingHealth{entered: make(chan struct{}), release: make(chan struct{})}
	grpcServer := grpc.NewServer()
	healthpb.RegisterHealthServer(grpcServer, health)
	requested, answer := make(chan struct{}), make(chan struct{})
	var requestOnce sync.Once
	mux := http.NewServeMux()
	mux.HandleFunc("GET /test-slow", func(w http.ResponseWriter, _ *http.Request) {
		requestOnce.Do(func() { close(requested) })
		<-answer
		_, _ = io.WriteString(w, "answered")
	})
	httpServer := &http.Server{Handler: mux}
	shared := graceful.NewShared(listener, grpcServer, httpServer)
	failed := make(chan error, 3)
	shared.Serve(failed)
	t.Cleanup(grpcServer.Stop)
	t.Cleanup(func() { _ = httpServer.Close() })

	connection, err := grpc.NewClient(tcp.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	result := make(chan call, 1)
	go func() {
		response, err := healthpb.NewHealthClient(connection).Check(t.Context(), &healthpb.HealthCheckRequest{})
		result <- call{response, err}
	}()
	answered := make(chan response, 1)
	go func() {
		got, err := http.Get("http://" + tcp.Addr().String() + "/test-slow")
		if err != nil {
			answered <- response{err: err}
			return
		}
		defer got.Body.Close()
		body, err := io.ReadAll(got.Body)
		answered <- response{string(body), err}
	}()
	for name, started := range map[string]<-chan struct{}{"gRPC call": health.entered, "HTTP request": requested} {
		select {
		case <-started:
		case <-time.After(waitLimit):
			t.Fatalf("the %s never reached its handler", name)
		}
	}

	stopped := make(chan struct{})
	go func() { shared.Shutdown(context.Background()); close(stopped) }()
	select {
	case <-listener.closed:
	case <-time.After(waitLimit):
		t.Fatal("shutting down did not close the listener")
	}
	select {
	case <-stopped:
		t.Fatal("Shutdown returned with a call and a request in flight")
	default:
	}

	close(answer)
	close(health.release)
	select {
	case got := <-answered:
		if got.err != nil || got.body != "answered" {
			t.Fatalf("the drained HTTP request = %q, %v; want its answer", got.body, got.err)
		}
	case <-time.After(waitLimit):
		t.Fatal("the drained HTTP request did not finish")
	}
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
		t.Fatal("Shutdown did not return once the call and the request finished")
	}
}

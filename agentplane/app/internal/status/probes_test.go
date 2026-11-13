package status

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func TestGRPCHealthProbeUsesServingStatus(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	done := make(chan struct{})
	go func() { defer close(done); _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); _ = listener.Close(); <-done })
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	checker, err := New([]Probe{GRPCHealthProbe("grpc", healthpb.NewHealthClient(connection))}, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	for _, serving := range []healthpb.HealthCheckResponse_ServingStatus{healthpb.HealthCheckResponse_SERVING, healthpb.HealthCheckResponse_NOT_SERVING} {
		healthServer.SetServingStatus("", serving)
		result := checker.CheckAll(t.Context())[0]
		if serving == healthpb.HealthCheckResponse_SERVING {
			if result.State != "ok" || result.Detail != nil {
				t.Fatalf("serving result = %+v", result)
			}
		} else if result.State != "down" || result.Detail == nil || !strings.Contains(*result.Detail, "NOT_SERVING") {
			t.Fatalf("not-serving result = %+v", result)
		}
	}
	server.Stop()
	result := checker.CheckAll(t.Context())[0]
	if result.State != "down" || result.Detail == nil || !strings.Contains(*result.Detail, "Unavailable") {
		t.Fatalf("unavailable result = %+v", result)
	}
}

func TestHTTPHealthProbeUsesStatusAndIndependentDeadline(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/ok":
			w.WriteHeader(http.StatusNoContent)
		case "/bad":
			w.WriteHeader(http.StatusServiceUnavailable)
		case "/wedged":
			<-r.Context().Done()
		}
	}))
	defer server.Close()
	checker, err := New([]Probe{
		HTTPHealthProbe("ok", server.Client(), server.URL+"/ok"),
		HTTPHealthProbe("bad", server.Client(), server.URL+"/bad"),
		HTTPHealthProbe("wedged", server.Client(), server.URL+"/wedged"),
	}, 200*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	results := checker.CheckAll(context.Background())
	if results[0].State != "ok" || results[0].Detail != nil {
		t.Fatalf("HTTP 204 = %+v", results[0])
	}
	if results[1].State != "down" || results[1].Detail == nil || !strings.Contains(*results[1].Detail, "HTTP 503") {
		t.Fatalf("HTTP 503 = %+v", results[1])
	}
	if results[2].State != "down" || results[2].Detail == nil || !strings.Contains(*results[2].Detail, "no answer within") {
		t.Fatalf("wedged HTTP = %+v", results[2])
	}
	server.Close()
	result := checker.CheckAll(t.Context())[0]
	if result.State != "down" || result.Detail == nil {
		t.Fatalf("unreachable HTTP = %+v", result)
	}
}

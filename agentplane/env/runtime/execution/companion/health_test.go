package executor

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	controlpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion/control/controlpb"
	"google.golang.org/grpc"
)

type unhealthyControl struct {
	controlpb.UnimplementedCompanionControlServer
	behavior string
	called   chan struct{}
}

func (s *unhealthyControl) Inspect(ctx context.Context, _ *controlpb.InspectRequest) (*controlpb.InspectResponse, error) {
	s.called <- struct{}{}
	if s.behavior == "stalled" {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	return &controlpb.InspectResponse{}, nil
}

func TestHealthUnavailable(t *testing.T) {
	for _, behavior := range []string{"absent", "malformed", "stalled"} {
		t.Run(behavior, func(t *testing.T) {
			socket := filepath.Join(t.TempDir(), "control.sock")
			var called chan struct{}
			if behavior != "absent" {
				listener, err := net.Listen("unix", socket)
				if err != nil {
					t.Fatal(err)
				}
				server := grpc.NewServer()
				called = make(chan struct{}, 1)
				controlpb.RegisterCompanionControlServer(server, &unhealthyControl{behavior: behavior, called: called})
				go func() { _ = server.Serve(listener) }()
				defer server.Stop()
			}
			response := httptest.NewRecorder()
			healthHandler(socket).ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/healthz", nil))
			if response.Code != http.StatusServiceUnavailable {
				t.Fatalf("health with %s control = %d, want 503", behavior, response.Code)
			}
			if called != nil {
				select {
				case <-called:
				default:
					t.Fatal("health did not call the control service")
				}
			}
		})
	}
}

func TestHealthExposesNoControlOperations(t *testing.T) {
	for _, request := range []struct {
		method string
		path   string
		status int
	}{
		{http.MethodPost, "/healthz", http.StatusMethodNotAllowed},
		{http.MethodGet, "/claim", http.StatusNotFound},
		{http.MethodGet, "/fence", http.StatusNotFound},
	} {
		response := httptest.NewRecorder()
		healthHandler("").ServeHTTP(response, httptest.NewRequest(request.method, request.path, nil))
		if response.Code != request.status {
			t.Errorf("%s %s = %d, want %d", request.method, request.path, response.Code, request.status)
		}
	}
}

package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/admission"
)

type facadeConnectionService struct {
	connection_servicepb.UnimplementedConnectionServiceServer
	called bool
}

func (s *facadeConnectionService) ListConnections(context.Context, *connection_servicepb.ListConnectionsRequest) (*connection_servicepb.ListConnectionsResponse, error) {
	s.called = true
	return &connection_servicepb.ListConnectionsResponse{}, nil
}

func TestConnectionHTTPHandlerRunsAdmissionGate(t *testing.T) {
	service := &facadeConnectionService{}
	handler, err := connectionHTTPHandler(context.Background(), service, admission.NewGate(nil))
	if err != nil {
		t.Fatal(err)
	}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/v1/operator/connections", nil)
	handler.ServeHTTP(recorder, request)

	if recorder.Code == http.StatusOK {
		t.Fatalf("unauthenticated request status = %d, want rejection", recorder.Code)
	}
	if service.called {
		t.Fatal("ConnectionService handled an unauthenticated request")
	}
}

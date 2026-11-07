package facade

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/v1/managementpb"
	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/v1/providerpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

type fakeManagement struct {
	managementpb.UnimplementedSessionManagementServer
	requests      chan *managementpb.CandidateRequest
	authorization chan string
}

func (s *fakeManagement) ImportSession(ctx context.Context, request *managementpb.CandidateRequest) (*providerpb.ImportSessionResponse, error) {
	s.requests <- request
	values := metadata.ValueFromIncomingContext(ctx, "authorization")
	if len(values) > 0 {
		s.authorization <- values[0]
	} else {
		s.authorization <- ""
	}
	return &providerpb.ImportSessionResponse{Result: &providerpb.ImportSessionResponse_Installed{Installed: &providerpb.SessionInstalled{}}}, nil
}

func TestGeneratedFacadeForwardsTypedImport(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	service := &fakeManagement{requests: make(chan *managementpb.CandidateRequest, 1), authorization: make(chan string, 1)}
	managementpb.RegisterSessionManagementServer(server, service)
	go server.Serve(listener)
	t.Cleanup(server.Stop)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	handler, err := New(ctx, listener.Addr().String(), time.Second*5)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/api/v1/instances/account:importSession", strings.NewReader(`{"mediaType":"application/json","formatVersion":"1","candidate":"e30="}`))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer test-owner")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	body, _ := io.ReadAll(response.Result().Body)
	if response.Code != http.StatusOK {
		t.Fatalf("status %d: %s", response.Code, body)
	}
	got := <-service.requests
	if got.InstanceId != "account" || string(got.Candidate) != "{}" || got.MediaType != "application/json" || got.FormatVersion != "1" {
		t.Fatalf("unexpected request: %v", got)
	}
	if got := <-service.authorization; got != "Bearer test-owner" {
		t.Fatalf("authorization: %q", got)
	}
}

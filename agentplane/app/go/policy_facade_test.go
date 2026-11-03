package main

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/proxy"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/emptypb"
)

type policyFacadeService struct {
	policypb.UnimplementedPolicyServiceServer
	request       *policypb.WatchPolicyBindingsRequest
	authorization []string
}

func (s *policyFacadeService) WatchPolicyBindings(request *policypb.WatchPolicyBindingsRequest, stream grpc.ServerStreamingServer[policypb.WatchPolicyBindingsResponse]) error {
	s.request = request
	md, _ := metadata.FromIncomingContext(stream.Context())
	s.authorization = md.Get("authorization")
	return stream.Send(&policypb.WatchPolicyBindingsResponse{Frame: &policypb.WatchPolicyBindingsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}})
}

func TestGeneratedPolicyFacadeUsesAppTokenAndSubject(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	service := &policyFacadeService{}
	policypb.RegisterPolicyServiceServer(server, service)
	done := make(chan struct{})
	go func() { defer close(done); _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); listener.Close(); <-done })
	handler, err := facade.NewHandler(t.Context(), listener.Addr().String(), time.Second, policypb.RegisterPolicyServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("policy-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	upstream := proxy.NewHandlerUpstream(handler, token)
	request := httptest.NewRequest(http.MethodGet, "/upstream/policy/v1/policy-namespaces/policies/policy-bindings:watch?subject.namespace=agents&subject.name=alice", nil)
	response := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(response, request, context.Background(), "v1/policy-namespaces/policies/policy-bindings:watch", ""); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"snapshotComplete"`) {
		t.Fatalf("response = %d %s", response.Code, response.Body.String())
	}
	if service.request.GetPolicyNamespace() != "policies" || service.request.GetSubject().GetNamespace() != "agents" || service.request.GetSubject().GetName() != "alice" {
		t.Fatalf("request = %v", service.request)
	}
	if len(service.authorization) != 1 || service.authorization[0] != "Bearer policy-token" {
		t.Fatalf("authorization = %v", service.authorization)
	}
}

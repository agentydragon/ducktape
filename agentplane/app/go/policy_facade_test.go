package main

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/proxy"
	egresspb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	rulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
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

// policyUpstream serves service behind the generated gateway, as the app's Policy Service facade does.
func policyUpstream(t *testing.T, service policypb.PolicyServiceServer) streamingUpstream {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
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
	return proxy.NewHandlerUpstream(handler, token)
}

func TestGeneratedPolicyFacadeUsesAppTokenAndSubject(t *testing.T) {
	service := &policyFacadeService{}
	upstream := policyUpstream(t, service)
	request := httptest.NewRequest(http.MethodGet, "/upstream/policy/v1/policy-bindings:watch?subject.namespace=agents&subject.name=alice", nil)
	response := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(response, request, context.Background(), "v1/policy-bindings:watch", ""); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"snapshotComplete"`) {
		t.Fatalf("response = %d %s", response.Code, response.Body.String())
	}
	if service.request.GetSubject().GetNamespace() != "agents" || service.request.GetSubject().GetName() != "alice" {
		t.Fatalf("request = %v", service.request)
	}
	if len(service.authorization) != 1 || service.authorization[0] != "Bearer policy-token" {
		t.Fatalf("authorization = %v", service.authorization)
	}
}

func TestGeneratedPolicyFacadeSelectsIdentityBindings(t *testing.T) {
	for _, tc := range []struct {
		query string
		check func(*policypb.WatchPolicyBindingsRequest) bool
	}{
		{"identity=fake-agent", func(r *policypb.WatchPolicyBindingsRequest) bool { return r.GetIdentity() == "fake-agent" }},
		{"everyIdentity=true", func(r *policypb.WatchPolicyBindingsRequest) bool { return r.GetEveryIdentity() }},
	} {
		t.Run(tc.query, func(t *testing.T) {
			service := &policyFacadeService{}
			upstream := policyUpstream(t, service)
			request := httptest.NewRequest(http.MethodGet, "/upstream/policy/v1/policy-bindings:watch?"+tc.query, nil)
			response := httptest.NewRecorder()
			if err := upstream.ForwardHTTP(response, request, context.Background(), "v1/policy-bindings:watch", ""); err != nil {
				t.Fatal(err)
			}
			if response.Code != http.StatusOK || !tc.check(service.request) {
				t.Fatalf("response = %d %s, request = %v", response.Code, response.Body.String(), service.request)
			}
		})
	}
}

type identityDeleteService struct {
	policypb.UnimplementedPolicyServiceServer
	request *policypb.DeleteIdentityRequest
}

func (s *identityDeleteService) DeleteIdentity(_ context.Context, request *policypb.DeleteIdentityRequest) (*policypb.DeleteIdentityResponse, error) {
	s.request = request
	refusal, err := status.New(codes.FailedPrecondition, "Identity is named by PolicyBindings it does not own").WithDetails(&errdetails.PreconditionFailure{
		Violations: []*errdetails.PreconditionFailure_Violation{{Type: "PolicyBinding", Subject: "standing-grant", Description: "names this Identity and is not owned by it"}},
	})
	if err != nil {
		return nil, err
	}
	return nil, refusal.Err()
}

// The app's identity page reads the referencing bindings from this body (frontend/identities/client.ts).
func TestGeneratedPolicyFacadeRelaysIdentityDeleteRefusal(t *testing.T) {
	service := &identityDeleteService{}
	upstream := policyUpstream(t, service)
	request := httptest.NewRequest(http.MethodDelete, "/upstream/policy/v1/identities/fake-policies/fake-agent?uid=uid-1", nil)
	response := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(response, request, context.Background(), "v1/identities/fake-policies/fake-agent", ""); err != nil {
		t.Fatal(err)
	}
	if service.request.GetNamespace() != "fake-policies" || service.request.GetName() != "fake-agent" || service.request.GetUid() != "uid-1" {
		t.Fatalf("request = %v", service.request)
	}
	var body struct {
		Code    int `json:"code"`
		Details []struct {
			Type       string `json:"@type"`
			Violations []struct {
				Type    string `json:"type"`
				Subject string `json:"subject"`
			} `json:"violations"`
		} `json:"details"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatalf("response = %d %s: %v", response.Code, response.Body.String(), err)
	}
	if response.Code != http.StatusBadRequest || body.Code != int(codes.FailedPrecondition) || len(body.Details) != 1 ||
		body.Details[0].Type != "type.googleapis.com/google.rpc.PreconditionFailure" || len(body.Details[0].Violations) != 1 ||
		body.Details[0].Violations[0].Type != "PolicyBinding" || body.Details[0].Violations[0].Subject != "standing-grant" {
		t.Fatalf("response = %d %s", response.Code, response.Body.String())
	}
}

type egressFacadeService struct {
	egresspb.UnimplementedEgressAdminServer
	authorization []string
}

func (s *egressFacadeService) WatchCredentials(_ *egresspb.WatchCredentialsRequest, stream grpc.ServerStreamingServer[egresspb.WatchCredentialsResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	s.authorization = md.Get("authorization")
	return stream.Send(&egresspb.WatchCredentialsResponse{Frame: &egresspb.WatchCredentialsResponse_Snapshot{Snapshot: &egresspb.CredentialSnapshot{Credentials: []*rulespb.Credential{{Name: "github", Uid: "uid", Description: "GitHub access"}}}}})
}

func TestGeneratedEgressFacadeUsesAppToken(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	service := &egressFacadeService{}
	egresspb.RegisterEgressAdminServer(server, service)
	done := make(chan struct{})
	go func() { defer close(done); _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); listener.Close(); <-done })
	handler, err := facade.NewHandler(t.Context(), listener.Addr().String(), time.Second, egresspb.RegisterEgressAdminHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("egress-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	upstream := proxy.NewHandlerUpstream(handler, token)
	request := httptest.NewRequest(http.MethodGet, "/upstream/egress/v1/credentials:watch", nil)
	response := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(response, request, context.Background(), "v1/credentials:watch", ""); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"description":"GitHub access"`) {
		t.Fatalf("response = %d %s", response.Code, response.Body.String())
	}
	if len(service.authorization) != 1 || service.authorization[0] != "Bearer egress-token" {
		t.Fatalf("authorization = %v", service.authorization)
	}
}

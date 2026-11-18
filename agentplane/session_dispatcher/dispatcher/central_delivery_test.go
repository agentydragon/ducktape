package dispatcher

import (
	"context"
	"crypto/x509"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placement"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type openingFixture struct{ opening *store.Session }

func (s openingFixture) GetSession(context.Context, string) (*store.Session, error) {
	if s.opening == nil {
		return nil, store.ErrNotFound
	}
	return s.opening, nil
}

type centralFixture struct {
	harnesshostpb.UnimplementedHarnessHostServer
	opened    *harnesshostpb.CreateSessionRequest
	token     string
	clientURI string
	requests  []proto.Message
	routing   metadata.MD
}

func (s *centralFixture) CreateSession(ctx context.Context, request *harnesshostpb.CreateSessionRequest) (*harnesshostpb.CreateSessionResponse, error) {
	s.opened = request
	s.requests = append(s.requests, request)
	md, _ := metadata.FromIncomingContext(ctx)
	s.routing = md
	if authorization := md.Get("authorization"); len(authorization) != 0 {
		s.token = authorization[0]
	}
	if remote, ok := peer.FromContext(ctx); ok {
		if tlsInfo, ok := remote.AuthInfo.(credentials.TLSInfo); ok && len(tlsInfo.State.PeerCertificates) != 0 {
			if uris := tlsInfo.State.PeerCertificates[0].URIs; len(uris) != 0 {
				s.clientURI = uris[0].String()
			}
		}
	}
	return &harnesshostpb.CreateSessionResponse{}, nil
}

func (s *centralFixture) InterruptTurn(_ context.Context, request *harnesshostpb.InterruptTurnRequest) (*harnesshostpb.InterruptTurnResponse, error) {
	s.requests = append(s.requests, request)
	return &harnesshostpb.InterruptTurnResponse{Admission: &eventlogpb.EventEntry{Cursor: 17}}, nil
}

func (s *centralFixture) SubmitInput(_ context.Context, r *harnesshostpb.SubmitInputRequest) (*harnesshostpb.SubmitInputResponse, error) {
	s.requests = append(s.requests, r)
	return &harnesshostpb.SubmitInputResponse{Admission: &eventlogpb.EventEntry{Cursor: 17}}, nil
}

func (s *centralFixture) ChangeModel(_ context.Context, r *harnesshostpb.ChangeModelRequest) (*harnesshostpb.ChangeModelResponse, error) {
	s.requests = append(s.requests, r)
	return &harnesshostpb.ChangeModelResponse{Admission: &eventlogpb.EventEntry{Cursor: 17}}, nil
}

func (s *centralFixture) ChangeReasoningEffort(_ context.Context, r *harnesshostpb.ChangeReasoningEffortRequest) (*harnesshostpb.ChangeReasoningEffortResponse, error) {
	s.requests = append(s.requests, r)
	return &harnesshostpb.ChangeReasoningEffortResponse{Admission: &eventlogpb.EventEntry{Cursor: 17}}, nil
}

func serveDeliveryHost(t *testing.T, fixture *centralFixture) string {
	t.Helper()
	return grpcserver.ServeRegistered(t, func(server *grpc.Server) {
		harnesshostpb.RegisterHarnessHostServer(server, fixture)
	})
}

func TestSessionDeliveryTargetsAndCommands(t *testing.T) {
	for _, kind := range []placement.SessionPlacement{placement.EnvironmentHosted, placement.CentralSampler} {
		t.Run(string(kind), func(t *testing.T) {
			proxy, sampler := &centralFixture{}, &centralFixture{}
			tokenFile := filepath.Join(t.TempDir(), "test-token")
			if err := os.WriteFile(tokenFile, []byte("test-service-credential"), 0600); err != nil {
				t.Fatal(err)
			}
			sandbox := SandboxSnapshot{Name: "test-executor", Namespace: "test", UID: "test-sandbox-uid", Incarnation: "test-pod-uid", Blocker: "sandbox_not_ready"}
			execution := store.SessionExecution{Identity: &identitypb.Identity{Name: "test-identity", Uid: "00000000-0000-4000-8000-00000000a008"}}
			spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: sandbox.UID}}}}}
			if kind == placement.EnvironmentHosted {
				spec.Placement.Kind = &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: sandbox.UID}, Harness: sessionpb.Harness_HARNESS_CODEX}}
			}
			router := &SessionDeliveryRouter{
				Host:   &HarnessHostClient{Namespace: sandbox.Namespace, TokenFile: tokenFile, TargetTemplate: serveDeliveryHost(t, proxy), Timeout: time.Second},
				Target: &CentralTarget{Target: serveDeliveryHost(t, sampler), TokenFile: tokenFile},
				Store:  openingFixture{&store.Session{SandboxName: sandbox.Name, SandboxUID: sandbox.UID, Placement: kind}},
			}
			setup := "printf test-setup"
			ctx := metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "untrusted-caller", sandboxapi.RuntimeProxySandboxUIDMetadataKey, "untrusted-sandbox"))
			if err := router.CreateSession(ctx, sandbox, "test-session", spec, &setup, execution); err != nil {
				t.Fatal(err)
			}
			selected, other := proxy, sampler
			if kind == placement.CentralSampler {
				selected, other = sampler, proxy
			}
			if len(selected.requests) != 1 || len(other.requests) != 0 {
				t.Fatalf("selected=%v other=%v", selected.requests, other.requests)
			}
			wantCreate := &harnesshostpb.CreateSessionRequest{SessionId: "test-session", Spec: spec, SetupScript: &setup, ExecutionIdentity: execution.Identity}
			if !proto.Equal(selected.opened, wantCreate) {
				t.Fatalf("creation = %v, want %v", selected.opened, wantCreate)
			}
			if selected.token != "Bearer test-service-credential" {
				t.Fatalf("caller credential forwarded: %q", selected.token)
			}
			for key, want := range map[string]string{
				sandboxapi.RuntimeProxySandboxMetadataKey:               sandbox.Name,
				sandboxapi.RuntimeProxySandboxNamespaceMetadataKey:      sandbox.Namespace,
				sandboxapi.RuntimeProxySandboxUIDMetadataKey:            sandbox.UID,
				sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey: sandbox.Namespace,
				sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey:      sandbox.Name,
			} {
				if kind == placement.CentralSampler {
					want = ""
				}
				if got := firstMetadataValue(selected.routing, key); got != want {
					t.Fatalf("routing %s = %q, want %q", key, got, want)
				}
			}
			for _, test := range []struct {
				command *commandpb.Command
				want    proto.Message
			}{
				{&commandpb.Command{CommandId: "input", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "test-input"}}}, &harnesshostpb.SubmitInputRequest{SessionId: "test-session", CommandId: "input", Text: "test-input"}},
				{&commandpb.Command{CommandId: "model", Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "test-model"}}}, &harnesshostpb.ChangeModelRequest{SessionId: "test-session", CommandId: "model", Model: "test-model"}},
				{&commandpb.Command{CommandId: "effort", Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: "high"}}}, &harnesshostpb.ChangeReasoningEffortRequest{SessionId: "test-session", CommandId: "effort", Effort: "high"}},
				{&commandpb.Command{CommandId: "interrupt", Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "test-turn"}}}, &harnesshostpb.InterruptTurnRequest{SessionId: "test-session", CommandId: "interrupt", TurnId: "test-turn"}},
			} {
				admission, err := router.Deliver(ctx, sandbox, "test-session", test.command)
				if err != nil || admission.GetCursor() != 17 {
					t.Fatalf("admission = %v, %v", admission, err)
				}
				if got := selected.requests[len(selected.requests)-1]; !proto.Equal(got, test.want) {
					t.Fatalf("command = %v, want %v", got, test.want)
				}
			}
			if len(other.requests) != 0 {
				t.Fatalf("other target called: %v", other.requests)
			}
			if _, err := router.Deliver(ctx, sandbox, "test-session", &commandpb.Command{}); status.Code(err) != codes.InvalidArgument {
				t.Fatalf("unsupported command = %v", err)
			}
			changed := sandbox
			changed.UID = "test-replacement"
			if _, err := router.Deliver(ctx, changed, "test-session", &commandpb.Command{}); status.Code(err) != codes.FailedPrecondition {
				t.Fatalf("replacement Sandbox = %v", err)
			}
			if err := router.CreateSession(ctx, changed, "test-session", spec, nil, execution); status.Code(err) != codes.FailedPrecondition {
				t.Fatalf("replacement creation = %v", err)
			}
			router.Store = openingFixture{}
			if _, err := router.Deliver(ctx, sandbox, "missing", &commandpb.Command{}); !errors.Is(err, store.ErrNotFound) {
				t.Fatalf("missing Session = %v", err)
			}
			if len(selected.requests) != 5 {
				t.Fatalf("rejected requests reached host: %v", selected.requests)
			}
		})
	}
}

func TestCentralDeliveryRequiresConfiguredTarget(t *testing.T) {
	sandbox := SandboxSnapshot{Name: "test-sandbox", UID: "test-uid", Incarnation: "test-pod"}
	router := &SessionDeliveryRouter{Host: &HarnessHostClient{Timeout: time.Second}, Store: openingFixture{&store.Session{SandboxName: sandbox.Name, SandboxUID: sandbox.UID, Placement: placement.CentralSampler}}}
	execution := store.SessionExecution{Identity: &identitypb.Identity{Name: "test-identity", Uid: "test-identity-uid"}}
	if err := router.CreateSession(t.Context(), sandbox, "test-session", nil, nil, execution); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("disabled creation = %v", err)
	}
	if _, err := router.Deliver(t.Context(), sandbox, "test-session", &commandpb.Command{}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("disabled delivery = %v", err)
	}
}

func TestCentralHarnessHostClientUsesSessionsSVIDWithoutBearerMetadata(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	serverCertificate := ca.IssueWithUsage(t, peertls.CentralSamplerURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := serverCertificate.WorkloadServerTLSConfigWithServices(peertls.CentralSamplerURI, true)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(serverTLS)))
	fixture := &centralFixture{}
	harnesshostpb.RegisterHarnessHostServer(server, fixture)
	serverAddr := grpcserver.Serve(t, server)

	caller := ca.Issue(t, peertls.SessionsURI)
	runtime := &HarnessHostClient{Timeout: time.Second}
	target := harnessHostTarget{address: serverAddr, tls: &peertls.ClientTLS{
		CAFile: ca.RootFile, ServerURI: peertls.CentralSamplerURI,
		CertFile: caller.CertFile, KeyFile: caller.KeyFile, ClientURI: peertls.SessionsURI,
	}}
	err = runtime.withClient(context.Background(), target, func(ctx context.Context, client harnesshostpb.HarnessHostClient) error {
		_, err := client.CreateSession(ctx, &harnesshostpb.CreateSessionRequest{})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if fixture.token != "" || fixture.clientURI != peertls.SessionsURI {
		t.Fatalf("sampler caller metadata=%q client SVID=%q", fixture.token, fixture.clientURI)
	}
}

type proxyRuntimeFixture struct {
	harnesshostpb.UnimplementedHarnessHostServer
	token        string
	clientURI    string
	sandboxName  string
	sandboxUID   string
	sandboxScope string
}

func (s *proxyRuntimeFixture) ListSessions(ctx context.Context, _ *harnesshostpb.ListSessionsRequest) (*harnesshostpb.ListSessionsResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if authorization := md.Get("authorization"); len(authorization) != 0 {
		s.token = authorization[0]
	}
	s.sandboxName = firstMetadataValue(md, sandboxapi.RuntimeProxySandboxMetadataKey)
	s.sandboxUID = firstMetadataValue(md, sandboxapi.RuntimeProxySandboxUIDMetadataKey)
	s.sandboxScope = firstMetadataValue(md, sandboxapi.RuntimeProxySandboxNamespaceMetadataKey)
	if remote, ok := peer.FromContext(ctx); ok {
		if tlsInfo, ok := remote.AuthInfo.(credentials.TLSInfo); ok && len(tlsInfo.State.PeerCertificates) != 0 {
			if uris := tlsInfo.State.PeerCertificates[0].URIs; len(uris) != 0 {
				s.clientURI = uris[0].String()
			}
		}
	}
	return &harnesshostpb.ListSessionsResponse{}, nil
}

func firstMetadataValue(md metadata.MD, key string) string {
	values := md.Get(key)
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

func TestHarnessHostClientUsesSessionsSVIDWithoutBearerMetadata(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	serverCertificate := ca.IssueWithUsage(t, peertls.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := serverCertificate.WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(serverTLS)))
	fixture := &proxyRuntimeFixture{}
	harnesshostpb.RegisterHarnessHostServer(server, fixture)
	serverAddr := grpcserver.Serve(t, server)

	caller := ca.Issue(t, peertls.SessionsURI)
	runtime := &HarnessHostClient{
		Namespace: "agentplane", Timeout: time.Second, TargetTemplate: serverAddr,
		GatewayTLS: &peertls.ClientTLS{
			CAFile: ca.RootFile, ServerURI: peertls.SessionDispatcherURI,
			CertFile: caller.CertFile, KeyFile: caller.KeyFile, ClientURI: peertls.SessionsURI,
		},
	}
	sandbox := SandboxSnapshot{Name: "sandbox", Namespace: "agentplane", UID: "sandbox-uid"}
	target, err := runtime.proxyTarget(sandbox)
	if err != nil {
		t.Fatal(err)
	}
	err = runtime.withClient(context.Background(), target, func(ctx context.Context, client harnesshostpb.HarnessHostClient) error {
		_, err := client.ListSessions(ctx, &harnesshostpb.ListSessionsRequest{})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if fixture.token != "" || fixture.clientURI != peertls.SessionsURI ||
		fixture.sandboxName != sandbox.Name || fixture.sandboxUID != sandbox.UID || fixture.sandboxScope != sandbox.Namespace {
		t.Fatalf("Gateway caller metadata=%q client SVID=%q sandbox=%q/%q/%q", fixture.token, fixture.clientURI,
			fixture.sandboxScope, fixture.sandboxName, fixture.sandboxUID)
	}
}

package dispatcher

import (
	"context"
	"crypto/x509"
	"errors"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/placement"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type openingFixture struct{ opening *Session }

func (s openingFixture) GetSession(context.Context, string) (*Session, error) {
	if s.opening == nil {
		return nil, ErrNotFound
	}
	return s.opening, nil
}

type nativeFixture struct{ calls int }

func (r *nativeFixture) CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, SessionExecution) error {
	r.calls++
	return nil
}

func (r *nativeFixture) Deliver(context.Context, SandboxSnapshot, string, *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.calls++
	return &eventlogpb.EventEntry{Cursor: 99}, nil
}

type centralFixture struct {
	samplerpb.UnimplementedSamplerServer
	opened      *samplerpb.CreateSessionRequest
	token       string
	clientURI   string
	interrupted string
}

func (s *centralFixture) CreateSession(ctx context.Context, request *samplerpb.CreateSessionRequest) (*samplerpb.CreateSessionResponse, error) {
	s.opened = request
	md, _ := metadata.FromIncomingContext(ctx)
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
	return &samplerpb.CreateSessionResponse{}, nil
}

func (s *centralFixture) InterruptTurn(_ context.Context, request *environmentruntimepb.InterruptTurnRequest) (*environmentruntimepb.InterruptTurnResponse, error) {
	s.interrupted = request.GetTurnId()
	return &environmentruntimepb.InterruptTurnResponse{Admission: &eventlogpb.EventEntry{Cursor: 17}}, nil
}

func TestRoutingKeepsCentralCommandsOffSandboxRunner(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	fixture := &centralFixture{}
	samplerpb.RegisterSamplerServer(server, fixture)
	go server.Serve(listener)
	t.Cleanup(server.Stop)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("projected\n"), 0600); err != nil {
		t.Fatal(err)
	}
	native := &nativeFixture{}
	sandbox := SandboxSnapshot{Name: "executor", Namespace: "test", UID: "sandbox-uid", Incarnation: "pod-uid", Blocker: "sandbox_not_ready"}
	spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: sandbox.UID}}}}}
	execution := SessionExecution{
		Identity: &identitypb.Identity{Name: "executor-identity", Uid: "00000000-0000-4000-8000-00000000a008"},
	}
	runtime := &RoutingRunner{Native: native, Store: openingFixture{&Session{
		SandboxName: sandbox.Name, SandboxUID: sandbox.UID, Placement: placement.CentralSampler, Spec: spec, ExecutionIdentity: execution.Identity,
	}}, Target: &CentralTarget{Target: listener.Addr().String(), TokenFile: tokenFile}, Timeout: time.Second}
	setup := "printf 'executor setup\\n'"
	if err := runtime.CreateSession(context.Background(), sandbox, "session", spec, &setup, execution); err != nil {
		t.Fatal(err)
	}
	if fixture.opened.GetExpectedIncarnationUid() != "pod-uid" || fixture.opened.GetExecutor().GetTarget().GetUid() != "sandbox-uid" || fixture.opened.GetSetupScript() != setup || fixture.token != "Bearer projected" ||
		!proto.Equal(fixture.opened.GetExecutionIdentity(), execution.Identity) {
		t.Fatalf("unexpected opening: %v token %q", fixture.opened, fixture.token)
	}
	admission, err := runtime.Deliver(context.Background(), sandbox, "session", &commandpb.Command{CommandId: "interrupt", Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "turn"}}})
	if err != nil || admission.GetCursor() != 17 || fixture.interrupted != "turn" || native.calls != 0 {
		t.Fatalf("interrupt %v %v native calls %d", admission, err, native.calls)
	}
	changed := sandbox
	changed.UID = "replacement"
	if _, err := runtime.Deliver(context.Background(), changed, "session", &commandpb.Command{}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("changed binding accepted: %v", err)
	}
	nativeSpec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: sandbox.UID}, Harness: sessionpb.Harness_HARNESS_CODEX}}}}
	runtime.Store = openingFixture{&Session{SandboxName: sandbox.Name, SandboxUID: sandbox.UID, Placement: placement.EnvironmentHosted, Spec: nativeSpec, ExecutionIdentity: execution.Identity}}
	if err := runtime.CreateSession(context.Background(), sandbox, "native", nativeSpec, nil, execution); err != nil {
		t.Fatal(err)
	}
	runtime.Store = openingFixture{}
	if _, err := runtime.Deliver(context.Background(), sandbox, "old-session", &commandpb.Command{}); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing Session routed to native Runtime: %v", err)
	}
	if native.calls != 1 {
		t.Fatalf("missing Session caused native delivery: %d calls", native.calls)
	}
	runtime.Store = openingFixture{&Session{SandboxName: sandbox.Name, SandboxUID: sandbox.UID, Placement: placement.EnvironmentHosted, Spec: nativeSpec, ExecutionIdentity: execution.Identity}}
	if _, err := runtime.Deliver(context.Background(), changed, "native", &commandpb.Command{}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("native Session routed to replacement Sandbox: %v", err)
	}
	if native.calls != 1 {
		t.Fatalf("replacement Sandbox caused native delivery: %d calls", native.calls)
	}
	if _, err := runtime.Deliver(context.Background(), sandbox, "native", &commandpb.Command{}); err != nil {
		t.Fatal(err)
	}
	if native.calls != 2 {
		t.Fatalf("native calls %d", native.calls)
	}
	runtime.Target = nil
	runtime.Store = openingFixture{&Session{SandboxName: sandbox.Name, SandboxUID: sandbox.UID, Placement: placement.CentralSampler, Spec: spec, ExecutionIdentity: execution.Identity}}
	if err := runtime.CreateSession(context.Background(), sandbox, "disabled", spec, nil, SessionExecution{}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("disabled sampler accepted: %v", err)
	}
}

func TestCentralSamplerClientVerifiesServerTLSAndKeepsBearerAuthentication(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	serverCertificate := ca.IssueWithUsage(t, peertls.CentralSamplerURI, x509.ExtKeyUsageServerAuth)
	serverCredentials, err := (peertls.ServerTLSListener{
		Listen: "127.0.0.1:0", CertFile: serverCertificate.CertFile, KeyFile: serverCertificate.KeyFile,
	}).Credentials(peertls.CentralSamplerURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(serverCredentials))
	fixture := &centralFixture{}
	samplerpb.RegisterSamplerServer(server, fixture)
	go server.Serve(listener)
	t.Cleanup(server.Stop)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("projected\n"), 0600); err != nil {
		t.Fatal(err)
	}
	runtime := &RoutingRunner{
		Target: &CentralTarget{
			Target: listener.Addr().String(), TokenFile: tokenFile,
			TLS: &peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.CentralSamplerURI},
		},
		Timeout: time.Second,
	}
	err = runtime.withClient(context.Background(), func(ctx context.Context, client samplerpb.SamplerClient) error {
		_, err := client.CreateSession(ctx, &samplerpb.CreateSessionRequest{})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if fixture.token != "Bearer projected" {
		t.Fatalf("sampler bearer authentication = %q", fixture.token)
	}
}

func TestCentralSamplerClientUsesSessionsSVIDWithoutBearerMetadata(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	serverCertificate := ca.IssueWithUsage(t, peertls.CentralSamplerURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := serverCertificate.WorkloadServerTLSConfigWithServices(peertls.CentralSamplerURI, true)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(serverTLS)))
	fixture := &centralFixture{}
	samplerpb.RegisterSamplerServer(server, fixture)
	go server.Serve(listener)
	t.Cleanup(server.Stop)

	caller := ca.Issue(t, peertls.SessionsURI)
	runtime := &RoutingRunner{
		Target: &CentralTarget{
			Target: listener.Addr().String(),
			TLS: &peertls.ClientTLS{
				CAFile: ca.RootFile, ServerURI: peertls.CentralSamplerURI,
				CertFile: caller.CertFile, KeyFile: caller.KeyFile, ClientURI: peertls.SessionsURI,
			},
		},
		Timeout: time.Second,
	}
	err = runtime.withClient(context.Background(), func(ctx context.Context, client samplerpb.SamplerClient) error {
		_, err := client.CreateSession(ctx, &samplerpb.CreateSessionRequest{})
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
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	token        string
	clientURI    string
	sandboxName  string
	sandboxUID   string
	sandboxScope string
}

func (s *proxyRuntimeFixture) ListSessions(ctx context.Context, _ *sessionpb.ListSessionsRequest) (*sessionpb.ListSessionsResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if authorization := md.Get("authorization"); len(authorization) != 0 {
		s.token = authorization[0]
	}
	s.sandboxName = firstMetadataValue(md, sandboxapi.RunnerProxySandboxMetadataKey)
	s.sandboxUID = firstMetadataValue(md, sandboxapi.RunnerProxySandboxUIDMetadataKey)
	s.sandboxScope = firstMetadataValue(md, sandboxapi.RunnerProxySandboxNamespaceMetadataKey)
	if remote, ok := peer.FromContext(ctx); ok {
		if tlsInfo, ok := remote.AuthInfo.(credentials.TLSInfo); ok && len(tlsInfo.State.PeerCertificates) != 0 {
			if uris := tlsInfo.State.PeerCertificates[0].URIs; len(uris) != 0 {
				s.clientURI = uris[0].String()
			}
		}
	}
	return &sessionpb.ListSessionsResponse{}, nil
}

func firstMetadataValue(md metadata.MD, key string) string {
	values := md.Get(key)
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

func TestProxyRunnerUsesSessionsSVIDWithoutBearerMetadata(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	serverCertificate := ca.IssueWithUsage(t, peertls.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := serverCertificate.WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(serverTLS)))
	fixture := &proxyRuntimeFixture{}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, fixture)
	go server.Serve(listener)
	t.Cleanup(server.Stop)

	caller := ca.Issue(t, peertls.SessionsURI)
	runtime := &ProxyRunner{
		Namespace: "agentplane", Timeout: time.Second, TargetTemplate: listener.Addr().String(),
		GatewayTLS: &peertls.ClientTLS{
			CAFile: ca.RootFile, ServerURI: peertls.SessionDispatcherURI,
			CertFile: caller.CertFile, KeyFile: caller.KeyFile, ClientURI: peertls.SessionsURI,
		},
	}
	sandbox := SandboxSnapshot{Name: "sandbox", Namespace: "agentplane", UID: "sandbox-uid"}
	err = runtime.withClient(context.Background(), sandbox, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		_, err := client.ListSessions(ctx, &sessionpb.ListSessionsRequest{})
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

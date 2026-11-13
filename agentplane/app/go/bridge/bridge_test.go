package bridge

import (
	"context"
	"errors"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type fakeSandboxGetter struct {
	sandbox *environmentpb.EnvironmentDescriptor
	err     error
	name    string
	uid     string
}

func (f *fakeSandboxGetter) GetEnvironment(_ context.Context, request *environmentpb.GetEnvironmentRequest, _ ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error) {
	target := request.GetTarget()
	f.name = target.GetName()
	f.uid = target.GetUid()
	return f.sandbox, f.err
}

type fakeDispatcher struct {
	openRequest *dispatcherpb.CreateSessionRequest
	opening     *dispatcherpb.Session
	err         error
}

func (f *fakeDispatcher) CreateSession(_ context.Context, request *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error) {
	f.openRequest = proto.Clone(request).(*dispatcherpb.CreateSessionRequest)
	if f.err != nil {
		return nil, f.err
	}
	return &dispatcherpb.CreateSessionResponse{Session: f.opening}, nil
}

type fakeRunner struct {
	list   *sessionpb.ListSessionsResponse
	status *sessionpb.EnvironmentRuntimeStatus
	err    error
	got    *environmentpb.EnvironmentDescriptor
}

func (f *fakeRunner) ListSessions(_ context.Context, sandbox *environmentpb.EnvironmentDescriptor) (*sessionpb.ListSessionsResponse, error) {
	f.got = sandbox
	return f.list, f.err
}

func (f *fakeRunner) EnvironmentRuntimeStatus(_ context.Context, sandbox *environmentpb.EnvironmentDescriptor) (*sessionpb.EnvironmentRuntimeStatus, error) {
	f.got = sandbox
	return f.status, f.err
}

func bridgeTestSpec() *sessionpb.SessionSpec {
	return &sessionpb.SessionSpec{Cwd: "/workspace", Model: "model", Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
		Environment: &sessionpb.ExactEnvironment{Name: "operator-sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CODEX,
	}}}}
}

func bridgeTestRequest(id string) *dispatcherpb.CreateSessionRequest {
	return &dispatcherpb.CreateSessionRequest{SessionId: id, Spec: bridgeTestSpec()}
}

func TestParseSessionSpecOverlaysDefaultsAndRejectsUnknownFields(t *testing.T) {
	defaults := &sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "operator-sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CODEX}}}, Cwd: "/workspace/{session_id}", Model: "default-model",
	}
	parsed, err := ParseSessionSpec(defaults, map[string]any{"model": "", "instructions": "follow this"})
	if err != nil {
		t.Fatal(err)
	}
	if parsed.GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CODEX || parsed.GetCwd() != defaults.GetCwd() || parsed.GetModel() != "" || parsed.GetInstructions() != "follow this" {
		t.Fatalf("parsed spec = %v", parsed)
	}
	if _, err := ParseSessionSpec(defaults, map[string]any{"not_a_field": true}); err == nil {
		t.Fatal("unknown proto-JSON field was accepted")
	} else {
		var malformed *MalformedMessageError
		if !errors.As(err, &malformed) {
			t.Fatalf("error = %T %v, want MalformedMessageError", err, err)
		}
	}
}

func TestCreateSessionPreservesIdentitySpecAndOptionalSetupScript(t *testing.T) {
	sessionID := uuid.MustParse("0a27b278-a057-48c5-8c46-10765b069e39")
	spec := bridgeTestSpec()
	setup := ""
	dispatcher := &fakeDispatcher{opening: &dispatcherpb.Session{
		SessionId: sessionID.String(), Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: spec.GetPlacement().GetEnvironmentHosted()}},
	}}
	bridge, err := New("operators", &fakeSandboxGetter{}, nil, dispatcher)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := bridge.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, spec, &setup)
	if err != nil {
		t.Fatal(err)
	}
	request := dispatcher.openRequest
	if request.GetSessionId() != sessionID.String() || request.GetSpec().GetPlacement().GetEnvironmentHosted().GetEnvironment().GetName() != "operator-sandbox" ||
		request.GetSpec().GetPlacement().GetEnvironmentHosted().GetEnvironment().GetUid() != "sandbox-uid" || !proto.Equal(request.GetSpec(), spec) {
		t.Fatalf("CreateSession request = %v", request)
	}
	if request.SetupScript == nil || request.GetSetupScript() != "" || opening != dispatcher.opening {
		t.Fatalf("setup script/opening not preserved: request=%v opening=%v", request, opening)
	}
	setup = "changed after call"
	if request.GetSetupScript() != "" {
		t.Fatalf("request retained caller's mutable setup pointer: %q", request.GetSetupScript())
	}

	dispatcher.opening = nil
	if _, err := bridge.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, spec, nil); err == nil {
		t.Fatal("missing or mismatched opening identity was accepted")
	} else {
		var notFound *SessionNotFoundError
		if !errors.As(err, &notFound) {
			t.Fatalf("error = %T %v, want SessionNotFoundError", err, err)
		}
	}
	if dispatcher.openRequest.SetupScript != nil {
		t.Fatalf("absent setup script should stay absent: %v", dispatcher.openRequest)
	}
	longScript := strings.Repeat("界", MaxSetupScriptCharacters+1)
	if _, err := bridge.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, spec, &longScript); err == nil {
		t.Fatal("overlong setup script was accepted")
	}
}

func TestCreateSessionForwardsNilUUIDLikePythonRoute(t *testing.T) {
	dispatcher := &fakeDispatcher{opening: &dispatcherpb.Session{
		SessionId: uuid.Nil.String(), Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: bridgeTestSpec().GetPlacement().GetEnvironmentHosted()}},
	}}
	bridge, err := New("operators", &fakeSandboxGetter{}, nil, dispatcher)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := bridge.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", uuid.Nil, bridgeTestSpec(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if dispatcher.openRequest.GetSessionId() != uuid.Nil.String() || dispatcher.openRequest.GetSpec().GetPlacement().GetEnvironmentHosted().GetEnvironment().GetUid() != "sandbox-uid" ||
		opening.GetSessionId() != uuid.Nil.String() {
		t.Fatalf("nil UUID was not forwarded: request=%v opening=%v", dispatcher.openRequest, opening)
	}
}

func TestCreateSessionTimeoutCarriesStableIdempotencyKey(t *testing.T) {
	sessionID := uuid.New()
	dispatcher := &fakeDispatcher{err: &RequestTimeoutError{Cause: context.DeadlineExceeded}}
	bridge, err := New("operators", &fakeSandboxGetter{}, nil, dispatcher)
	if err != nil {
		t.Fatal(err)
	}
	_, err = bridge.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, bridgeTestSpec(), nil)
	var timeout *DispatcherRequestTimeoutError
	if !errors.As(err, &timeout) || timeout.SessionID != sessionID || !strings.Contains(err.Error(), sessionID.String()) {
		t.Fatalf("timeout = %#v, error = %v", timeout, err)
	}
}

func TestRunnerRoutesUseCurrentSandboxIdentityAndReturnProxiedValues(t *testing.T) {
	sandbox := &environmentpb.EnvironmentDescriptor{Target: &identitypb.EnvironmentTarget{Name: "operator-sandbox", Uid: "sandbox-uid", Namespace: "operators"}}
	list := &sessionpb.ListSessionsResponse{Sessions: []*sessionpb.SessionSummary{{SessionId: uuid.NewString()}}}
	status := &sessionpb.EnvironmentRuntimeStatus{EnvironmentRuntimeBootId: "boot-1"}
	runner := &fakeRunner{list: list, status: status}
	getter := &fakeSandboxGetter{sandbox: sandbox}
	bridge, err := New("operators", getter, runner, &fakeDispatcher{})
	if err != nil {
		t.Fatal(err)
	}
	sessions, err := bridge.ListSessions(context.Background(), sandbox.GetTarget().GetName(), sandbox.GetTarget().GetUid())
	if err != nil || !proto.Equal(&sessionpb.ListSessionsResponse{Sessions: sessions}, list) {
		t.Fatalf("ListSessions = %v, %v", sessions, err)
	}
	if getter.name != sandbox.GetTarget().GetName() || runner.got != sandbox {
		t.Fatalf("sandbox lookup/proxy identity = %q / %p, want %q / %p", getter.name, runner.got, sandbox.GetTarget().GetName(), sandbox)
	}
	gotStatus, err := bridge.EnvironmentRuntimeStatus(context.Background(), sandbox.GetTarget().GetName(), sandbox.GetTarget().GetUid())
	if err != nil || !proto.Equal(gotStatus, status) {
		t.Fatalf("EnvironmentRuntimeStatus = %v, %v", gotStatus, err)
	}
	if getter.uid != "sandbox-uid" || runner.got.GetTarget().GetUid() != "sandbox-uid" {
		t.Fatalf("runtime status lost UID: lookup=%q target=%v", getter.uid, runner.got.GetTarget())
	}
	getter.sandbox = &environmentpb.EnvironmentDescriptor{Target: &identitypb.EnvironmentTarget{Name: sandbox.GetTarget().GetName(), Uid: "replacement", Namespace: sandbox.GetTarget().GetNamespace()}}
	if _, err := bridge.EnvironmentRuntimeStatus(context.Background(), sandbox.GetTarget().GetName(), sandbox.GetTarget().GetUid()); err == nil {
		t.Fatal("same-name replacement was proxied")
	}
	getter.sandbox = &environmentpb.EnvironmentDescriptor{Target: &identitypb.EnvironmentTarget{Name: sandbox.GetTarget().GetName(), Namespace: sandbox.GetTarget().GetNamespace()}}
	if _, err := bridge.ListSessions(context.Background(), sandbox.GetTarget().GetName(), sandbox.GetTarget().GetUid()); err == nil {
		t.Fatal("Sandbox without UID was proxied")
	}
}

type identityEnvironmentRuntimeServer struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	t    *testing.T
	seen map[string]string
}

func (s *identityEnvironmentRuntimeServer) ListSessions(ctx context.Context, _ *sessionpb.ListSessionsRequest) (*sessionpb.ListSessionsResponse, error) {
	s.capture(ctx)
	return &sessionpb.ListSessionsResponse{Sessions: []*sessionpb.SessionSummary{{SessionId: "durable-session"}}}, nil
}

func (s *identityEnvironmentRuntimeServer) GetStatus(ctx context.Context, _ *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	s.capture(ctx)
	return &sessionpb.EnvironmentRuntimeStatus{EnvironmentRuntimeBootId: "live-boot"}, nil
}

func (s *identityEnvironmentRuntimeServer) capture(ctx context.Context) {
	s.t.Helper()
	values, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		s.t.Fatal("runner call has no metadata")
	}
	for _, key := range []string{
		"authorization", sandboxapi.RunnerProxySandboxMetadataKey,
		sandboxapi.RunnerProxySandboxNamespaceMetadataKey,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey,
	} {
		entries := values.Get(key)
		if len(entries) != 1 {
			s.t.Fatalf("metadata %q = %v", key, entries)
		}
		s.seen[key] = entries[0]
	}
}

func TestGatewayRunnerProxyBindsCurrentSandboxMetadata(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	service := &identityEnvironmentRuntimeServer{t: t, seen: make(map[string]string)}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)

	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("rotated-runner-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	proxy, err := NewGatewayRunnerProxy(RunnerConfig{
		Namespace: "operators", TokenFile: tokenFile, Timeout: time.Second,
		TargetTemplate: listener.Addr().String(),
	})
	if err != nil {
		t.Fatal(err)
	}
	sandbox := &environmentpb.EnvironmentDescriptor{Target: &identitypb.EnvironmentTarget{Name: "operator-sandbox", Uid: "sandbox-uid", Namespace: "operators"}}
	listed, err := proxy.ListSessions(context.Background(), sandbox)
	if err != nil || listed.GetSessions()[0].GetSessionId() != "durable-session" {
		t.Fatalf("ListSessions = %v, %v", listed, err)
	}
	runnerStatus, err := proxy.EnvironmentRuntimeStatus(context.Background(), sandbox)
	if err != nil || runnerStatus.GetEnvironmentRuntimeBootId() != "live-boot" {
		t.Fatalf("EnvironmentRuntimeStatus = %v, %v", runnerStatus, err)
	}
	want := map[string]string{
		"authorization":                                        "Bearer rotated-runner-token",
		sandboxapi.RunnerProxySandboxMetadataKey:               "operator-sandbox",
		sandboxapi.RunnerProxySandboxNamespaceMetadataKey:      "operators",
		sandboxapi.RunnerProxySandboxUIDMetadataKey:            "sandbox-uid",
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey: "operators",
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey:      "operator-sandbox",
	}
	for key, value := range want {
		if service.seen[key] != value {
			t.Errorf("metadata %q = %q, want %q", key, service.seen[key], value)
		}
	}
}

type dispatcherTestServer struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	t          *testing.T
	blockOpen  bool
	seenBearer []string
}

func (s *dispatcherTestServer) capture(ctx context.Context) {
	s.t.Helper()
	values, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		s.t.Fatal("dispatcher call has no metadata")
	}
	entries := values.Get("authorization")
	if len(entries) != 1 {
		s.t.Fatalf("authorization metadata = %v", entries)
	}
	s.seenBearer = append(s.seenBearer, entries[0])
}

func (s *dispatcherTestServer) CreateSession(ctx context.Context, request *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error) {
	s.capture(ctx)
	if s.blockOpen {
		<-ctx.Done()
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	return &dispatcherpb.CreateSessionResponse{Session: &dispatcherpb.Session{
		SessionId: request.GetSessionId(), Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: request.GetSpec().GetPlacement().GetEnvironmentHosted()}},
	}}, nil
}

func TestDispatcherClientUsesRotatingBearerAndMapsDeadline(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	service := &dispatcherTestServer{t: t}
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)

	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("token-one\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := NewDispatcherClient(DispatcherConfig{Target: listener.Addr().String(), TokenFile: tokenFile, Timeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	id := uuid.NewString()
	if _, err := client.CreateSession(context.Background(), bridgeTestRequest(id)); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tokenFile, []byte("token-two\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := client.CreateSession(context.Background(), bridgeTestRequest(id)); err != nil {
		t.Fatal(err)
	}
	if len(service.seenBearer) != 2 || service.seenBearer[0] != "Bearer token-one" || service.seenBearer[1] != "Bearer token-two" {
		t.Fatalf("dispatcher bearer values = %v", service.seenBearer)
	}

	service.blockOpen = true
	slowClient, err := NewDispatcherClient(DispatcherConfig{Target: listener.Addr().String(), TokenFile: tokenFile, Timeout: 20 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	_, err = slowClient.CreateSession(context.Background(), bridgeTestRequest(id))
	var timeout *RequestTimeoutError
	if !errors.As(err, &timeout) {
		t.Fatalf("deadline error = %T %v, want RequestTimeoutError", err, err)
	}
}

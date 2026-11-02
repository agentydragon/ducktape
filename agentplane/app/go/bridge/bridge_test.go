package bridge

import (
	"context"
	"errors"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type fakeSandboxGetter struct {
	sandbox *sandboxespb.Sandbox
	err     error
	name    string
}

func (f *fakeSandboxGetter) Get(_ context.Context, name string) (*sandboxespb.Sandbox, error) {
	f.name = name
	return f.sandbox, f.err
}

type fakeDispatcher struct {
	openRequest *dispatcherpb.OpenSessionRequest
	opening     *dispatcherpb.SessionOpening
	err         error
}

func (f *fakeDispatcher) OpenSession(_ context.Context, request *dispatcherpb.OpenSessionRequest) (*dispatcherpb.OpenSessionResponse, error) {
	f.openRequest = proto.Clone(request).(*dispatcherpb.OpenSessionRequest)
	if f.err != nil {
		return nil, f.err
	}
	return &dispatcherpb.OpenSessionResponse{Opening: f.opening}, nil
}

func (f *fakeDispatcher) GetSessionOpening(_ context.Context, _ *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error) {
	if f.err != nil {
		return nil, f.err
	}
	return &dispatcherpb.GetSessionOpeningResponse{Opening: f.opening}, nil
}

type fakeRunner struct {
	list   *sessionpb.ListSessionsResponse
	status *sessionpb.EnvironmentRuntimeStatus
	err    error
	got    *sandboxespb.Sandbox
}

func (f *fakeRunner) ListSessions(_ context.Context, sandbox *sandboxespb.Sandbox) (*sessionpb.ListSessionsResponse, error) {
	f.got = sandbox
	return f.list, f.err
}

func (f *fakeRunner) EnvironmentRuntimeStatus(_ context.Context, sandbox *sandboxespb.Sandbox) (*sessionpb.EnvironmentRuntimeStatus, error) {
	f.got = sandbox
	return f.status, f.err
}

func TestParseSessionSpecOverlaysDefaultsAndRejectsUnknownFields(t *testing.T) {
	defaults := &sessionpb.SessionSpec{
		Harness: sessionpb.Harness_HARNESS_CODEX, Cwd: "/workspace/{session_id}", Model: "default-model",
	}
	parsed, err := ParseSessionSpec(defaults, map[string]any{"model": "", "instructions": "follow this"})
	if err != nil {
		t.Fatal(err)
	}
	if parsed.GetHarness() != sessionpb.Harness_HARNESS_CODEX || parsed.GetCwd() != defaults.GetCwd() || parsed.GetModel() != "" || parsed.GetInstructions() != "follow this" {
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

func TestOpenSessionPreservesIdentitySpecAndOptionalSetupScript(t *testing.T) {
	sessionID := uuid.MustParse("0a27b278-a057-48c5-8c46-10765b069e39")
	spec := &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CODEX, Cwd: "/workspace", Model: "model"}
	setup := ""
	dispatcher := &fakeDispatcher{opening: &dispatcherpb.SessionOpening{
		SessionId: sessionID.String(), Sandbox: "operator-sandbox", SandboxUid: "sandbox-uid",
	}}
	bridge, err := New(&fakeSandboxGetter{}, nil, dispatcher)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := bridge.OpenSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, spec, &setup)
	if err != nil {
		t.Fatal(err)
	}
	request := dispatcher.openRequest
	if request.GetSessionId() != sessionID.String() || request.GetSandbox() != "operator-sandbox" ||
		request.GetSandboxUid() != "sandbox-uid" || !proto.Equal(request.GetSpec(), spec) {
		t.Fatalf("OpenSession request = %v", request)
	}
	if request.SetupScript == nil || request.GetSetupScript() != "" || opening != dispatcher.opening {
		t.Fatalf("setup script/opening not preserved: request=%v opening=%v", request, opening)
	}
	setup = "changed after call"
	if request.GetSetupScript() != "" {
		t.Fatalf("request retained caller's mutable setup pointer: %q", request.GetSetupScript())
	}

	dispatcher.opening = nil
	if _, err := bridge.OpenSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, spec, nil); err == nil {
		t.Fatal("missing or mismatched opening identity was accepted")
	} else {
		var notFound *SessionOpeningNotFoundError
		if !errors.As(err, &notFound) {
			t.Fatalf("error = %T %v, want SessionOpeningNotFoundError", err, err)
		}
	}
	if dispatcher.openRequest.SetupScript != nil {
		t.Fatalf("absent setup script should stay absent: %v", dispatcher.openRequest)
	}
	longScript := strings.Repeat("界", MaxSetupScriptCharacters+1)
	if _, err := bridge.OpenSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, spec, &longScript); err == nil {
		t.Fatal("overlong setup script was accepted")
	}
}

func TestOpenSessionForwardsNilUUIDLikePythonRoute(t *testing.T) {
	dispatcher := &fakeDispatcher{opening: &dispatcherpb.SessionOpening{
		SessionId: uuid.Nil.String(), Sandbox: "operator-sandbox", SandboxUid: "sandbox-uid",
	}}
	bridge, err := New(&fakeSandboxGetter{}, nil, dispatcher)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := bridge.OpenSession(context.Background(), "operator-sandbox", "sandbox-uid", uuid.Nil, &sessionpb.SessionSpec{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if dispatcher.openRequest.GetSessionId() != uuid.Nil.String() || dispatcher.openRequest.GetSandboxUid() != "sandbox-uid" ||
		opening.GetSessionId() != uuid.Nil.String() {
		t.Fatalf("nil UUID was not forwarded: request=%v opening=%v", dispatcher.openRequest, opening)
	}
}

func TestOpenSessionTimeoutCarriesStableIdempotencyKey(t *testing.T) {
	sessionID := uuid.New()
	dispatcher := &fakeDispatcher{err: &RequestTimeoutError{Cause: context.DeadlineExceeded}}
	bridge, err := New(&fakeSandboxGetter{}, nil, dispatcher)
	if err != nil {
		t.Fatal(err)
	}
	_, err = bridge.OpenSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, &sessionpb.SessionSpec{}, nil)
	var timeout *DispatcherRequestTimeoutError
	if !errors.As(err, &timeout) || timeout.SessionID != sessionID || !strings.Contains(err.Error(), sessionID.String()) {
		t.Fatalf("timeout = %#v, error = %v", timeout, err)
	}
}

func TestSessionOpeningHidesWrongSandboxAndSession(t *testing.T) {
	sessionID := uuid.New()
	for _, opening := range []*dispatcherpb.SessionOpening{
		{SessionId: sessionID.String(), Sandbox: "another-sandbox"},
		{SessionId: uuid.NewString(), Sandbox: "operator-sandbox"},
		{},
	} {
		bridge, err := New(&fakeSandboxGetter{}, nil, &fakeDispatcher{opening: opening})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := bridge.SessionOpening(context.Background(), "operator-sandbox", sessionID); err == nil {
			t.Fatalf("opening %v was returned for the wrong identity", opening)
		} else {
			var notFound *SessionOpeningNotFoundError
			if !errors.As(err, &notFound) {
				t.Fatalf("error = %T %v, want SessionOpeningNotFoundError", err, err)
			}
		}
	}
}

func TestRunnerRoutesUseCurrentSandboxIdentityAndReturnProxiedValues(t *testing.T) {
	sandbox := &sandboxespb.Sandbox{Name: "operator-sandbox", Uid: "sandbox-uid", Namespace: "operators"}
	list := &sessionpb.ListSessionsResponse{Sessions: []*sessionpb.SessionSummary{{SessionId: uuid.NewString()}}}
	status := &sessionpb.EnvironmentRuntimeStatus{EnvironmentRuntimeBootId: "boot-1"}
	runner := &fakeRunner{list: list, status: status}
	getter := &fakeSandboxGetter{sandbox: sandbox}
	bridge, err := New(getter, runner, &fakeDispatcher{})
	if err != nil {
		t.Fatal(err)
	}
	sessions, err := bridge.ListSessions(context.Background(), sandbox.GetName())
	if err != nil || !proto.Equal(&sessionpb.ListSessionsResponse{Sessions: sessions}, list) {
		t.Fatalf("ListSessions = %v, %v", sessions, err)
	}
	if getter.name != sandbox.GetName() || runner.got != sandbox {
		t.Fatalf("sandbox lookup/proxy identity = %q / %p, want %q / %p", getter.name, runner.got, sandbox.GetName(), sandbox)
	}
	gotStatus, err := bridge.EnvironmentRuntimeStatus(context.Background(), sandbox.GetName())
	if err != nil || !proto.Equal(gotStatus, status) {
		t.Fatalf("EnvironmentRuntimeStatus = %v, %v", gotStatus, err)
	}
	getter.sandbox = &sandboxespb.Sandbox{Name: sandbox.GetName(), Namespace: sandbox.GetNamespace()}
	if _, err := bridge.ListSessions(context.Background(), sandbox.GetName()); err == nil {
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
	sandbox := &sandboxespb.Sandbox{Name: "operator-sandbox", Uid: "sandbox-uid", Namespace: "operators"}
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

func (s *dispatcherTestServer) OpenSession(ctx context.Context, request *dispatcherpb.OpenSessionRequest) (*dispatcherpb.OpenSessionResponse, error) {
	s.capture(ctx)
	if s.blockOpen {
		<-ctx.Done()
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	return &dispatcherpb.OpenSessionResponse{Opening: &dispatcherpb.SessionOpening{
		SessionId: request.GetSessionId(), Sandbox: request.GetSandbox(), SandboxUid: request.GetSandboxUid(),
	}}, nil
}

func (s *dispatcherTestServer) GetSessionOpening(ctx context.Context, request *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error) {
	s.capture(ctx)
	return &dispatcherpb.GetSessionOpeningResponse{Opening: &dispatcherpb.SessionOpening{SessionId: request.GetSessionId(), Sandbox: "operator-sandbox"}}, nil
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
	if _, err := client.OpenSession(context.Background(), &dispatcherpb.OpenSessionRequest{SessionId: id, Sandbox: "operator-sandbox", SandboxUid: "sandbox-uid"}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetSessionOpening(context.Background(), &dispatcherpb.GetSessionOpeningRequest{SessionId: id}); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tokenFile, []byte("token-two\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetSessionOpening(context.Background(), &dispatcherpb.GetSessionOpeningRequest{SessionId: id}); err != nil {
		t.Fatal(err)
	}
	if len(service.seenBearer) != 3 || service.seenBearer[0] != "Bearer token-one" || service.seenBearer[2] != "Bearer token-two" {
		t.Fatalf("dispatcher bearer values = %v", service.seenBearer)
	}

	service.blockOpen = true
	slowClient, err := NewDispatcherClient(DispatcherConfig{Target: listener.Addr().String(), TokenFile: tokenFile, Timeout: 20 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	_, err = slowClient.OpenSession(context.Background(), &dispatcherpb.OpenSessionRequest{SessionId: id, Sandbox: "operator-sandbox", SandboxUid: "sandbox-uid"})
	var timeout *RequestTimeoutError
	if !errors.As(err, &timeout) {
		t.Fatalf("deadline error = %T %v, want RequestTimeoutError", err, err)
	}
}

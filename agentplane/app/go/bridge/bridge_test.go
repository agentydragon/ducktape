package bridge

import (
	"context"
	"errors"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
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
	requests    []any
	err         error
	commandErr  error
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

func (f *fakeDispatcher) SubmitInput(_ context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	f.requests = append(f.requests, proto.Clone(request))
	if f.commandErr != nil {
		return nil, f.commandErr
	}
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (f *fakeDispatcher) ChangeModel(_ context.Context, request *dispatcherpb.ChangeModelRequest) (*dispatcherpb.ChangeModelResponse, error) {
	f.requests = append(f.requests, proto.Clone(request))
	if f.commandErr != nil {
		return nil, f.commandErr
	}
	return &dispatcherpb.ChangeModelResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (f *fakeDispatcher) ChangeReasoningEffort(_ context.Context, request *dispatcherpb.ChangeReasoningEffortRequest) (*dispatcherpb.ChangeReasoningEffortResponse, error) {
	f.requests = append(f.requests, proto.Clone(request))
	if f.commandErr != nil {
		return nil, f.commandErr
	}
	return &dispatcherpb.ChangeReasoningEffortResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (f *fakeDispatcher) InterruptTurn(_ context.Context, request *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
	f.requests = append(f.requests, proto.Clone(request))
	if f.commandErr != nil {
		return nil, f.commandErr
	}
	return &dispatcherpb.InterruptTurnResponse{State: dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED}, nil
}

type fakeRunner struct {
	list   *runnerpb.ListSessionsResponse
	status *runnerpb.RunnerStatus
	err    error
	got    *sandboxespb.Sandbox
}

func (f *fakeRunner) ListSessions(_ context.Context, sandbox *sandboxespb.Sandbox) (*runnerpb.ListSessionsResponse, error) {
	f.got = sandbox
	return f.list, f.err
}

func (f *fakeRunner) RunnerStatus(_ context.Context, sandbox *sandboxespb.Sandbox) (*runnerpb.RunnerStatus, error) {
	f.got = sandbox
	return f.status, f.err
}

type fakeReadiness struct {
	exists   bool
	snapshot *FeedSnapshot
	err      error
	feedErr  error
}

func (f *fakeReadiness) Exists(context.Context, uuid.UUID) (bool, error) { return f.exists, f.err }
func (f *fakeReadiness) FeedState(context.Context, uuid.UUID) (*FeedSnapshot, error) {
	return f.snapshot, f.feedErr
}

func TestParseSessionSpecOverlaysDefaultsAndRejectsUnknownFields(t *testing.T) {
	defaults := &runnerpb.SessionSpec{
		Harness: runnerpb.Harness_HARNESS_CODEX, Cwd: "/workspace/{session_id}", Model: "default-model",
	}
	parsed, err := ParseSessionSpec(defaults, map[string]any{"model": "", "instructions": "follow this"})
	if err != nil {
		t.Fatal(err)
	}
	if parsed.GetHarness() != runnerpb.Harness_HARNESS_CODEX || parsed.GetCwd() != defaults.GetCwd() || parsed.GetModel() != "" || parsed.GetInstructions() != "follow this" {
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
	spec := &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CODEX, Cwd: "/workspace", Model: "model"}
	setup := ""
	dispatcher := &fakeDispatcher{opening: &dispatcherpb.SessionOpening{
		SessionId: sessionID.String(), Sandbox: "operator-sandbox",
	}}
	bridge, err := New(&fakeSandboxGetter{}, nil, dispatcher, nil)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := bridge.OpenSession(context.Background(), "operator-sandbox", sessionID, spec, &setup)
	if err != nil {
		t.Fatal(err)
	}
	request := dispatcher.openRequest
	if request.GetSessionId() != sessionID.String() || request.GetSandbox() != "operator-sandbox" || !proto.Equal(request.GetSpec(), spec) {
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
	if _, err := bridge.OpenSession(context.Background(), "operator-sandbox", sessionID, spec, nil); err == nil {
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
	if _, err := bridge.OpenSession(context.Background(), "operator-sandbox", sessionID, spec, &longScript); err == nil {
		t.Fatal("overlong setup script was accepted")
	}
}

func TestOpenSessionForwardsNilUUIDLikePythonRoute(t *testing.T) {
	dispatcher := &fakeDispatcher{opening: &dispatcherpb.SessionOpening{
		SessionId: uuid.Nil.String(), Sandbox: "operator-sandbox",
	}}
	bridge, err := New(&fakeSandboxGetter{}, nil, dispatcher, nil)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := bridge.OpenSession(context.Background(), "operator-sandbox", uuid.Nil, &runnerpb.SessionSpec{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if dispatcher.openRequest.GetSessionId() != uuid.Nil.String() || opening.GetSessionId() != uuid.Nil.String() {
		t.Fatalf("nil UUID was not forwarded: request=%v opening=%v", dispatcher.openRequest, opening)
	}
}

func TestOpenSessionTimeoutCarriesStableIdempotencyKey(t *testing.T) {
	sessionID := uuid.New()
	dispatcher := &fakeDispatcher{err: &RequestTimeoutError{Cause: context.DeadlineExceeded}}
	bridge, err := New(&fakeSandboxGetter{}, nil, dispatcher, nil)
	if err != nil {
		t.Fatal(err)
	}
	_, err = bridge.OpenSession(context.Background(), "operator-sandbox", sessionID, &runnerpb.SessionSpec{}, nil)
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
		bridge, err := New(&fakeSandboxGetter{}, nil, &fakeDispatcher{opening: opening}, nil)
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

func TestCommandReadyChecksThreadAndRejectedFeed(t *testing.T) {
	threadID := uuid.New()
	bridge, err := New(&fakeSandboxGetter{}, nil, &fakeDispatcher{}, &fakeReadiness{})
	if err != nil {
		t.Fatal(err)
	}
	if err := bridge.CommandReady(context.Background(), threadID); err == nil {
		t.Fatal("missing Thread was admitted")
	} else {
		var missing *ThreadNotFoundError
		if !errors.As(err, &missing) || missing.ThreadID != threadID {
			t.Fatalf("error = %T %v, want ThreadNotFoundError", err, err)
		}
	}
	readiness := &fakeReadiness{exists: true, snapshot: &FeedSnapshot{End: &FeedError{Message: "cursor 17 was rejected"}}}
	bridge, err = New(&fakeSandboxGetter{}, nil, &fakeDispatcher{}, readiness)
	if err != nil {
		t.Fatal(err)
	}
	if err := bridge.CommandReady(context.Background(), threadID); err == nil || err.Error() != "runner history is rejected: cursor 17 was rejected" {
		t.Fatalf("rejected feed error = %v", err)
	}
	readiness.snapshot = nil
	if err := bridge.CommandReady(context.Background(), threadID); err != nil {
		t.Fatalf("nil feed snapshot should be admissible: %v", err)
	}
}

func TestCommandMethodsCheckReadinessAndPreservePayloads(t *testing.T) {
	sessionID := uuid.New()
	dispatcher := &fakeDispatcher{}
	bridge, err := New(&fakeSandboxGetter{}, nil, dispatcher, &fakeReadiness{exists: true})
	if err != nil {
		t.Fatal(err)
	}
	input, err := bridge.SubmitInput(context.Background(), "operator-sandbox", sessionID, "input-1", "hello")
	if err != nil || input.GetCommandId() != "input-1" {
		t.Fatalf("SubmitInput = %v, %v", input, err)
	}
	model, err := bridge.ChangeModel(context.Background(), "operator-sandbox", sessionID, "model-1", "vendor/model")
	if err != nil || model.GetCommandId() != "model-1" {
		t.Fatalf("ChangeModel = %v, %v", model, err)
	}
	effort, err := bridge.ChangeReasoningEffort(context.Background(), "operator-sandbox", sessionID, "effort-1", "high")
	if err != nil || effort.GetCommandId() != "effort-1" {
		t.Fatalf("ChangeReasoningEffort = %v, %v", effort, err)
	}
	interrupted, err := bridge.InterruptTurn(context.Background(), "operator-sandbox", sessionID, "interrupt-1", "turn-7")
	if err != nil || interrupted.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED {
		t.Fatalf("InterruptTurn = %v, %v", interrupted, err)
	}
	if len(dispatcher.requests) != 4 {
		t.Fatalf("got %d dispatcher calls, want 4", len(dispatcher.requests))
	}
	if got := dispatcher.requests[0].(*dispatcherpb.SubmitInputRequest); got.GetSandbox() != "operator-sandbox" || got.GetSessionId() != sessionID.String() || got.GetText() != "hello" {
		t.Fatalf("SubmitInput request = %v", got)
	}
	if got := dispatcher.requests[1].(*dispatcherpb.ChangeModelRequest); got.GetModel() != "vendor/model" {
		t.Fatalf("ChangeModel request = %v", got)
	}
	if got := dispatcher.requests[2].(*dispatcherpb.ChangeReasoningEffortRequest); got.GetEffort() != "high" {
		t.Fatalf("ChangeReasoningEffort request = %v", got)
	}
	if got := dispatcher.requests[3].(*dispatcherpb.InterruptTurnRequest); got.GetTurnId() != "turn-7" {
		t.Fatalf("InterruptTurn request = %v", got)
	}

	blockedDispatcher := &fakeDispatcher{}
	blocked, err := New(&fakeSandboxGetter{}, nil, blockedDispatcher, &fakeReadiness{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := blocked.SubmitInput(context.Background(), "operator-sandbox", sessionID, "blocked", "hello"); err == nil {
		t.Fatal("command for a missing Thread reached the Dispatcher")
	}
	if len(blockedDispatcher.requests) != 0 {
		t.Fatalf("blocked command reached Dispatcher: %v", blockedDispatcher.requests)
	}
}

func TestCommandTimeoutCarriesCommandIDAndGRPCStatusesAreTyped(t *testing.T) {
	sessionID := uuid.New()
	dispatcher := &fakeDispatcher{commandErr: &RequestTimeoutError{Cause: context.DeadlineExceeded}}
	bridge, err := New(&fakeSandboxGetter{}, nil, dispatcher, &fakeReadiness{exists: true})
	if err != nil {
		t.Fatal(err)
	}
	_, err = bridge.SubmitInput(context.Background(), "operator-sandbox", sessionID, "stable-command-id", "hello")
	var timeout *DispatcherRequestTimeoutError
	if !errors.As(err, &timeout) || timeout.CommandID != "stable-command-id" || !strings.Contains(err.Error(), "command 'stable-command-id'") {
		t.Fatalf("timeout = %#v, error = %v", timeout, err)
	}
	if _, ok := translatedRPCError(status.Error(codes.FailedPrecondition, "runner refused")).(*RefusedError); !ok {
		t.Fatal("FAILED_PRECONDITION did not map to RefusedError")
	}
	if _, ok := translatedRPCError(status.Error(codes.Unavailable, "runner offline")).(*ServiceError); !ok {
		t.Fatal("UNAVAILABLE did not map to ServiceError")
	}
	if _, ok := translatedRPCError(status.Error(codes.DeadlineExceeded, "deadline")).(*RequestTimeoutError); !ok {
		t.Fatal("DEADLINE_EXCEEDED did not map to RequestTimeoutError")
	}
}

func TestRunnerRoutesUseCurrentSandboxIdentityAndReturnProxiedValues(t *testing.T) {
	sandbox := &sandboxespb.Sandbox{Name: "operator-sandbox", Uid: "sandbox-uid", Namespace: "operators"}
	list := &runnerpb.ListSessionsResponse{Sessions: []*runnerpb.SessionSummary{{SessionId: uuid.NewString()}}}
	status := &runnerpb.RunnerStatus{RunnerBootId: "boot-1"}
	runner := &fakeRunner{list: list, status: status}
	getter := &fakeSandboxGetter{sandbox: sandbox}
	bridge, err := New(getter, runner, &fakeDispatcher{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	sessions, err := bridge.ListSessions(context.Background(), sandbox.GetName())
	if err != nil || !proto.Equal(&runnerpb.ListSessionsResponse{Sessions: sessions}, list) {
		t.Fatalf("ListSessions = %v, %v", sessions, err)
	}
	if getter.name != sandbox.GetName() || runner.got != sandbox {
		t.Fatalf("sandbox lookup/proxy identity = %q / %p, want %q / %p", getter.name, runner.got, sandbox.GetName(), sandbox)
	}
	gotStatus, err := bridge.RunnerStatus(context.Background(), sandbox.GetName())
	if err != nil || !proto.Equal(gotStatus, status) {
		t.Fatalf("RunnerStatus = %v, %v", gotStatus, err)
	}
	getter.sandbox = &sandboxespb.Sandbox{Name: sandbox.GetName(), Namespace: sandbox.GetNamespace()}
	if _, err := bridge.ListSessions(context.Background(), sandbox.GetName()); err == nil {
		t.Fatal("Sandbox without UID was proxied")
	}
}

type identityRunnerServer struct {
	runnerservicepb.UnimplementedRunnerServer
	t    *testing.T
	seen map[string]string
}

func (s *identityRunnerServer) ListSessions(ctx context.Context, _ *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
	s.capture(ctx)
	return &runnerpb.ListSessionsResponse{Sessions: []*runnerpb.SessionSummary{{SessionId: "durable-session"}}}, nil
}

func (s *identityRunnerServer) GetStatus(ctx context.Context, _ *runnerpb.GetStatusRequest) (*runnerpb.RunnerStatus, error) {
	s.capture(ctx)
	return &runnerpb.RunnerStatus{RunnerBootId: "live-boot"}, nil
}

func (s *identityRunnerServer) capture(ctx context.Context) {
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
	service := &identityRunnerServer{t: t, seen: make(map[string]string)}
	runnerservicepb.RegisterRunnerServer(server, service)
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
	runnerStatus, err := proxy.RunnerStatus(context.Background(), sandbox)
	if err != nil || runnerStatus.GetRunnerBootId() != "live-boot" {
		t.Fatalf("RunnerStatus = %v, %v", runnerStatus, err)
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
		SessionId: request.GetSessionId(), Sandbox: request.GetSandbox(),
	}}, nil
}

func (s *dispatcherTestServer) GetSessionOpening(ctx context.Context, request *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error) {
	s.capture(ctx)
	return &dispatcherpb.GetSessionOpeningResponse{Opening: &dispatcherpb.SessionOpening{SessionId: request.GetSessionId(), Sandbox: "operator-sandbox"}}, nil
}

func (s *dispatcherTestServer) SubmitInput(ctx context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	s.capture(ctx)
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (s *dispatcherTestServer) ChangeModel(ctx context.Context, request *dispatcherpb.ChangeModelRequest) (*dispatcherpb.ChangeModelResponse, error) {
	s.capture(ctx)
	return &dispatcherpb.ChangeModelResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (s *dispatcherTestServer) ChangeReasoningEffort(ctx context.Context, request *dispatcherpb.ChangeReasoningEffortRequest) (*dispatcherpb.ChangeReasoningEffortResponse, error) {
	s.capture(ctx)
	return &dispatcherpb.ChangeReasoningEffortResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (s *dispatcherTestServer) InterruptTurn(ctx context.Context, _ *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
	s.capture(ctx)
	return &dispatcherpb.InterruptTurnResponse{State: dispatcherpb.DeliveryState_DELIVERY_STATE_NO_OP}, nil
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
	if _, err := client.OpenSession(context.Background(), &dispatcherpb.OpenSessionRequest{SessionId: id, Sandbox: "operator-sandbox"}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.SubmitInput(context.Background(), &dispatcherpb.SubmitInputRequest{CommandId: "input", SessionId: id, Sandbox: "operator-sandbox"}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.ChangeModel(context.Background(), &dispatcherpb.ChangeModelRequest{CommandId: "model", SessionId: id, Sandbox: "operator-sandbox"}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.ChangeReasoningEffort(context.Background(), &dispatcherpb.ChangeReasoningEffortRequest{CommandId: "effort", SessionId: id, Sandbox: "operator-sandbox"}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.InterruptTurn(context.Background(), &dispatcherpb.InterruptTurnRequest{CommandId: "interrupt", SessionId: id, Sandbox: "operator-sandbox"}); err != nil {
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
	if len(service.seenBearer) != 7 || service.seenBearer[0] != "Bearer token-one" || service.seenBearer[6] != "Bearer token-two" {
		t.Fatalf("dispatcher bearer values = %v", service.seenBearer)
	}

	service.blockOpen = true
	slowClient, err := NewDispatcherClient(DispatcherConfig{Target: listener.Addr().String(), TokenFile: tokenFile, Timeout: 20 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	_, err = slowClient.OpenSession(context.Background(), &dispatcherpb.OpenSessionRequest{SessionId: id, Sandbox: "operator-sandbox"})
	var timeout *RequestTimeoutError
	if !errors.As(err, &timeout) {
		t.Fatalf("deadline error = %T %v, want RequestTimeoutError", err, err)
	}
}

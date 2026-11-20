package sessionclient

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type fakeSessionDispatcher struct {
	openRequest *dispatcherpb.CreateSessionRequest
	opening     *dispatcherpb.Session
	err         error
}

func (f *fakeSessionDispatcher) CreateSession(_ context.Context, request *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error) {
	f.openRequest = proto.Clone(request).(*dispatcherpb.CreateSessionRequest)
	if f.err != nil {
		return nil, f.err
	}
	return &dispatcherpb.CreateSessionResponse{Session: f.opening}, nil
}

func sessionClientTestSpec() *sessionpb.SessionSpec {
	return &sessionpb.SessionSpec{Cwd: "/workspace", Model: "model", Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
		Environment: &sessionpb.ExactEnvironment{Name: "operator-sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CODEX,
	}}}}
}

func sessionClientTestRequest(id string) *dispatcherpb.CreateSessionRequest {
	return &dispatcherpb.CreateSessionRequest{SessionId: id, Spec: sessionClientTestSpec()}
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
	spec := sessionClientTestSpec()
	setup := ""
	dispatcher := &fakeSessionDispatcher{opening: &dispatcherpb.Session{
		SessionId: sessionID.String(), Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: spec.GetPlacement().GetEnvironmentHosted()}},
	}}
	client, err := New(dispatcher)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := client.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, spec, &setup)
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
	if _, err := client.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, spec, nil); err == nil {
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
	if _, err := client.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, spec, &longScript); err == nil {
		t.Fatal("overlong setup script was accepted")
	}
}

func TestCreateSessionForwardsNilUUID(t *testing.T) {
	dispatcher := &fakeSessionDispatcher{opening: &dispatcherpb.Session{
		SessionId: uuid.Nil.String(), Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: sessionClientTestSpec().GetPlacement().GetEnvironmentHosted()}},
	}}
	client, err := New(dispatcher)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := client.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", uuid.Nil, sessionClientTestSpec(), nil)
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
	dispatcher := &fakeSessionDispatcher{err: &RequestTimeoutError{Cause: context.DeadlineExceeded}}
	client, err := New(dispatcher)
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.CreateSession(context.Background(), "operator-sandbox", "sandbox-uid", sessionID, sessionClientTestSpec(), nil)
	var timeout *DispatcherRequestTimeoutError
	if !errors.As(err, &timeout) || timeout.SessionID != sessionID || !strings.Contains(err.Error(), sessionID.String()) {
		t.Fatalf("timeout = %#v, error = %v", timeout, err)
	}
}

type appSVIDDispatcherServer struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	t        *testing.T
	calls    int
	getError error
}

func (s *appSVIDDispatcherServer) CreateSession(ctx context.Context, request *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error) {
	s.capture(ctx)
	return &dispatcherpb.CreateSessionResponse{Session: &dispatcherpb.Session{
		SessionId: request.GetSessionId(),
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: request.GetSpec().GetPlacement().GetEnvironmentHosted()}},
	}}, nil
}

func (s *appSVIDDispatcherServer) capture(ctx context.Context) {
	s.t.Helper()
	identity, err := peertls.PeerIdentity(ctx)
	if err != nil || identity != peertls.AppURI {
		s.t.Fatalf("Session Dispatcher peer identity = %q, error %v", identity, err)
	}
	values, ok := metadata.FromIncomingContext(ctx)
	if !ok || len(values.Get("authorization")) != 0 {
		s.t.Fatalf("Session Dispatcher received authorization metadata: %v", values)
	}
	s.calls++
}

func (s *appSVIDDispatcherServer) GetSession(ctx context.Context, request *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error) {
	s.capture(ctx)
	if s.getError != nil {
		return nil, s.getError
	}
	return &dispatcherpb.GetSessionResponse{Session: &dispatcherpb.Session{SessionId: request.GetSessionId()}}, nil
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

func TestSessionDispatcherClientUsesRotatingBearerAndMapsDeadline(t *testing.T) {
	server := grpc.NewServer()
	service := &dispatcherTestServer{t: t}
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	address := grpcserver.Serve(t, server)

	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("token-one\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := NewSessionDispatcherClient(SessionDispatcherConfig{Target: address, TokenFile: tokenFile, Timeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	id := uuid.NewString()
	if _, err := client.CreateSession(context.Background(), sessionClientTestRequest(id)); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tokenFile, []byte("token-two\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := client.CreateSession(context.Background(), sessionClientTestRequest(id)); err != nil {
		t.Fatal(err)
	}
	if len(service.seenBearer) != 2 || service.seenBearer[0] != "Bearer token-one" || service.seenBearer[1] != "Bearer token-two" {
		t.Fatalf("dispatcher bearer values = %v", service.seenBearer)
	}

	service.blockOpen = true
	slowClient, err := NewSessionDispatcherClient(SessionDispatcherConfig{Target: address, TokenFile: tokenFile, Timeout: 20 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	_, err = slowClient.CreateSession(context.Background(), sessionClientTestRequest(id))
	var timeout *RequestTimeoutError
	if !errors.As(err, &timeout) {
		t.Fatalf("deadline error = %T %v, want RequestTimeoutError", err, err)
	}
}

func TestSessionDispatcherClientAuthenticatesAppSVIDWithoutBearerAndDoesNotFallBack(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	serverConfig := ca.Issue(t, peertls.SessionDispatcherURI)
	serverCredentials, err := serverConfig.ServerCredentials(peertls.AppURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(serverCredentials))
	service := &appSVIDDispatcherServer{t: t}
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	address := grpcserver.Serve(t, server)

	appCertificate := ca.Issue(t, peertls.AppURI)
	clientTLS := &peertls.ClientTLS{
		CAFile: ca.RootFile, ServerURI: peertls.SessionDispatcherURI,
		CertFile: appCertificate.CertFile, KeyFile: appCertificate.KeyFile, ClientURI: peertls.AppURI,
	}
	client, err := NewSessionDispatcherClient(SessionDispatcherConfig{
		TLS: clientTLS, Target: address, Timeout: time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	id := uuid.NewString()
	if _, err := client.CreateSession(context.Background(), sessionClientTestRequest(id)); err != nil {
		t.Fatalf("SVID-authenticated CreateSession: %v", err)
	}
	read, err := client.GetSession(context.Background(), &dispatcherpb.GetSessionRequest{SessionId: id})
	if err != nil || read.GetSession().GetSessionId() != id {
		t.Fatalf("SVID-authenticated GetSession: %v, %v", read, err)
	}
	service.getError = status.Error(codes.FailedPrecondition, "test activity unavailable")
	if _, err := client.GetSession(context.Background(), &dispatcherpb.GetSessionRequest{SessionId: id}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("GetSession status = %v, want FailedPrecondition", err)
	}
	if service.calls != 3 {
		t.Fatalf("Session Dispatcher calls = %d, want 3", service.calls)
	}

	missingCertificate := *clientTLS
	missingCertificate.CertFile = t.TempDir() + "/missing.crt"
	failedClient, err := NewSessionDispatcherClient(SessionDispatcherConfig{
		TLS: &missingCertificate, Target: address, Timeout: time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := failedClient.CreateSession(context.Background(), sessionClientTestRequest(uuid.NewString())); err == nil {
		t.Fatal("CreateSession succeeded after its configured SVID certificate became unavailable")
	}
	if service.calls != 3 {
		t.Fatalf("failed SVID call reached the service; calls = %d", service.calls)
	}
}

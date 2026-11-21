package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	operatoractionpb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	webpushpb "github.com/agentydragon/ducktape/agentplane/action_service/api/web_push/web_push_servicepb"
	upstreamproxy "github.com/agentydragon/ducktape/agentplane/app/internal/upstream"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	connectionpb "github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	notificationpb "github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type generatedEnvironmentService struct {
	environmentpb.UnimplementedEnvironmentServiceServer
	authorization       []string
	created             *environmentpb.CreateEnvironmentRequest
	deleted             *identitypb.EnvironmentTarget
	recordTarget        *identitypb.EnvironmentTarget
	identityChange      *environmentpb.SetEnvironmentIdentityRequest
	diagnosticsTarget   *identitypb.EnvironmentTarget
	diagnosticsResponse *environmentpb.GetRuntimeDiagnosticsResponse
	diagnosticsError    error
	diagnosticsPeer     string
	diagnosticsAuth     []string
	diagnosticsCookie   []string
}

func (s *generatedEnvironmentService) CreateEnvironment(ctx context.Context, request *environmentpb.CreateEnvironmentRequest) (*identitypb.EnvironmentTarget, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	s.authorization = append(s.authorization, md.Get("authorization")...)
	s.created = request
	return &identitypb.EnvironmentTarget{Namespace: "agentplane", Name: request.GetName(), Uid: "sandbox-uid"}, nil
}

func (s *generatedEnvironmentService) DeleteEnvironment(ctx context.Context, request *environmentpb.EnvironmentMutationRequest) (*emptypb.Empty, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	s.authorization = append(s.authorization, md.Get("authorization")...)
	s.deleted = request.GetTarget()
	return &emptypb.Empty{}, nil
}

func (s *generatedEnvironmentService) GetEnvironmentIdentityRecord(ctx context.Context, request *environmentpb.GetEnvironmentRequest) (*environmentpb.EnvironmentIdentityRecord, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	s.authorization = append(s.authorization, md.Get("authorization")...)
	s.recordTarget = request.GetTarget()
	return &environmentpb.EnvironmentIdentityRecord{Identity: &identitypb.Identity{Name: "test-agent", Uid: "identity-uid"}}, nil
}

func (s *generatedEnvironmentService) GetRuntimeDiagnostics(ctx context.Context, request *environmentpb.GetRuntimeDiagnosticsRequest) (*environmentpb.GetRuntimeDiagnosticsResponse, error) {
	peer, err := peertls.PeerIdentity(ctx)
	if err != nil || peer != peertls.AppURI {
		return nil, status.Error(codes.Unauthenticated, "App SVID is required")
	}
	s.diagnosticsPeer = peer
	md, _ := metadata.FromIncomingContext(ctx)
	s.diagnosticsAuth = append(s.diagnosticsAuth, md.Get("authorization")...)
	s.diagnosticsCookie = append(s.diagnosticsCookie, md.Get("cookie")...)
	if len(md.Get("authorization")) != 0 || len(md.Get("cookie")) != 0 {
		return nil, status.Error(codes.Unauthenticated, "browser credentials must not be forwarded")
	}
	s.diagnosticsTarget = request.GetTarget()
	if s.diagnosticsError != nil {
		return nil, s.diagnosticsError
	}
	return s.diagnosticsResponse, nil
}

func (s *generatedEnvironmentService) SetEnvironmentIdentity(ctx context.Context, request *environmentpb.SetEnvironmentIdentityRequest) (*emptypb.Empty, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	s.authorization = append(s.authorization, md.Get("authorization")...)
	s.identityChange = request
	return &emptypb.Empty{}, nil
}

func TestGeneratedEnvironmentFacadeRoutesManagementWithAppServiceToken(t *testing.T) {
	service := &generatedEnvironmentService{}
	server := grpc.NewServer()
	environmentpb.RegisterEnvironmentServiceServer(server, service)
	address := grpcserver.Serve(t, server)
	handler, err := facade.NewHandler(t.Context(), address, 10*time.Second, environmentpb.RegisterEnvironmentServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	tokenFile := filepath.Join(t.TempDir(), "environment-token")
	if err := os.WriteFile(tokenFile, []byte("app-pod-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	upstream := upstreamproxy.NewHandlerUpstream(handler, tokenFile)
	for _, operation := range []struct{ method, url, body, path string }{
		{http.MethodPost, "/upstream/environments/v1/environments", `{"name":"created","template":"pod"}`, "v1/environments"},
		{http.MethodDelete, "/upstream/environments/v1/environments/agentplane/created?target.uid=sandbox-uid", "", "v1/environments/agentplane/created"},
		{http.MethodGet, "/upstream/environments/v1/environments/agentplane/created:identity-record?target.uid=sandbox-uid", "", "v1/environments/agentplane/created:identity-record"},
		{http.MethodPatch, "/upstream/environments/v1/environments/agentplane/created:identity", `{"target":{"namespace":"agentplane","name":"created","uid":"sandbox-uid"},"identityName":"test-agent"}`, "v1/environments/agentplane/created:identity"},
	} {
		request := httptest.NewRequest(operation.method, operation.url, strings.NewReader(operation.body))
		request.Header.Set("Content-Type", "application/json")
		response := httptest.NewRecorder()
		if err := upstream.ForwardHTTP(response, request, request.Context(), operation.path, ""); err != nil {
			t.Fatal(err)
		}
		if response.Code != http.StatusOK {
			t.Fatalf("%s %s = %d: %s", operation.method, operation.url, response.Code, response.Body.String())
		}
	}
	if service.created.GetName() != "created" || service.created.GetTemplate() != "pod" ||
		service.deleted.GetNamespace() != "agentplane" || service.deleted.GetName() != "created" || service.deleted.GetUid() != "sandbox-uid" ||
		service.recordTarget.GetNamespace() != "agentplane" || service.recordTarget.GetName() != "created" || service.recordTarget.GetUid() != "sandbox-uid" ||
		service.identityChange.GetTarget().GetUid() != "sandbox-uid" || service.identityChange.GetIdentityName() != "test-agent" {
		t.Fatalf("generated facade requests: create=%+v delete=%+v record=%+v change=%+v", service.created, service.deleted, service.recordTarget, service.identityChange)
	}
	if len(service.authorization) != 4 || service.authorization[0] != "Bearer app-pod-token" || service.authorization[1] != "Bearer app-pod-token" || service.authorization[2] != "Bearer app-pod-token" || service.authorization[3] != "Bearer app-pod-token" {
		t.Fatalf("generated facade authorization = %v", service.authorization)
	}
}

func TestGeneratedEnvironmentFacadeReturnsRuntimeDiagnosticsEnvelopeAndErrors(t *testing.T) {
	observedAt := time.Date(2026, time.November, 16, 12, 34, 56, 0, time.UTC)
	target := &identitypb.EnvironmentTarget{Namespace: "team-a", Name: "sandbox-1", Uid: "sandbox-uid"}
	service := &generatedEnvironmentService{diagnosticsResponse: &environmentpb.GetRuntimeDiagnosticsResponse{
		Target: target, IncarnationUid: "pod-uid", ObservedAt: timestamppb.New(observedAt),
		Status: &sessionpb.EnvironmentRuntimeStatus{
			EnvironmentRuntimeBootId: "boot-id",
			Processes:                []*sessionpb.HarnessProcessStatus{{ProcessId: "process-1", AssignedSessionIds: []string{"session-1"}}},
		},
	}}
	upstream := appMTLSFacade(t, peertls.SandboxControllerURI, func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	}, environmentpb.RegisterEnvironmentServiceHandlerFromEndpoint)
	request := httptest.NewRequest(
		http.MethodGet,
		"/upstream/environments/v1/environments/team-a/sandbox-1:runtime-diagnostics?target.uid=sandbox-uid",
		nil,
	)
	request.Header.Set("Authorization", "Bearer browser-token")
	request.Header.Set("Cookie", "session=browser-cookie")
	response := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(response, request, request.Context(), "v1/environments/team-a/sandbox-1:runtime-diagnostics", ""); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK {
		t.Fatalf("diagnostics facade = %d: %s", response.Code, response.Body.String())
	}
	got := &environmentpb.GetRuntimeDiagnosticsResponse{}
	if err := protojson.Unmarshal(response.Body.Bytes(), got); err != nil {
		t.Fatalf("decode diagnostics facade response %q: %v", response.Body.String(), err)
	}
	if !proto.Equal(got, service.diagnosticsResponse) {
		t.Fatalf("diagnostics facade response = %v, want full envelope %v", got, service.diagnosticsResponse)
	}
	if service.diagnosticsTarget == nil || service.diagnosticsTarget.GetNamespace() != target.GetNamespace() ||
		service.diagnosticsTarget.GetName() != target.GetName() || service.diagnosticsTarget.GetUid() != target.GetUid() {
		t.Fatalf("diagnostics target = %v", service.diagnosticsTarget)
	}
	if service.diagnosticsPeer != peertls.AppURI || len(service.diagnosticsAuth) != 0 || len(service.diagnosticsCookie) != 0 {
		t.Fatalf("diagnostics peer=%q authorization=%v cookies=%v", service.diagnosticsPeer, service.diagnosticsAuth, service.diagnosticsCookie)
	}

	service.diagnosticsError = status.Error(codes.NotFound, "environment not found")
	request = httptest.NewRequest(
		http.MethodGet,
		"/upstream/environments/v1/environments/team-a/sandbox-1:runtime-diagnostics?target.uid=sandbox-uid",
		nil,
	)
	response = httptest.NewRecorder()
	if err := upstream.ForwardHTTP(response, request, request.Context(), "v1/environments/team-a/sandbox-1:runtime-diagnostics", ""); err != nil {
		t.Fatal(err)
	}
	var gatewayError struct {
		Code int `json:"code"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &gatewayError); err != nil {
		t.Fatalf("decode diagnostics gateway error %q: %v", response.Body.String(), err)
	}
	if response.Code != http.StatusNotFound || gatewayError.Code != int(codes.NotFound) {
		t.Fatalf("diagnostics error = %d: %s", response.Code, response.Body.String())
	}
	if service.diagnosticsPeer != peertls.AppURI || len(service.diagnosticsAuth) != 0 || len(service.diagnosticsCookie) != 0 {
		t.Fatalf("diagnostics error peer=%q authorization=%v cookies=%v", service.diagnosticsPeer, service.diagnosticsAuth, service.diagnosticsCookie)
	}
}

type generatedActionService struct {
	actionpb.UnimplementedActionServiceServer

	authorization  []string
	clientIdentity []string
	cookie         []string
	privateHeader  []string
	started        chan struct{}
}

type generatedConnectionService struct {
	connectionpb.UnimplementedConnectionServiceServer
	authorization  []string
	clientIdentity []string
}

func (s *generatedConnectionService) ListConnections(ctx context.Context, _ *connectionpb.ListConnectionsRequest) (*connectionpb.ListConnectionsResponse, error) {
	identity, err := peertls.PeerIdentity(ctx)
	if err != nil || identity != peertls.AppURI {
		return nil, status.Error(codes.Unauthenticated, "App SVID is required")
	}
	s.clientIdentity = append(s.clientIdentity, identity)
	md, _ := metadata.FromIncomingContext(ctx)
	authorization := md.Get("authorization")
	s.authorization = append(s.authorization, strings.Join(authorization, ","))
	if len(authorization) != 1 || authorization[0] != "Bearer connection-token" {
		return nil, status.Error(codes.Unauthenticated, "operator bearer is required")
	}
	return &connectionpb.ListConnectionsResponse{}, nil
}

type generatedDispatcherService struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	authorization []string
	unaryStarted  chan struct{}
	unaryCanceled chan struct{}
	watchStarted  chan struct{}
	watchCanceled chan struct{}
}

type generatedMetadataDispatcherService struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
}

type generatedNotificationService struct {
	notificationpb.UnimplementedNotificationsServer
	sessionID     string
	authorization []string
	sendCalled    bool
}

func (s *generatedNotificationService) SendMessage(_ context.Context, _ *notificationpb.SendMessageRequest) (*notificationpb.SendMessageResponse, error) {
	s.sendCalled = true
	return &notificationpb.SendMessageResponse{}, nil
}

func (s *generatedNotificationService) WatchInbox(request *notificationpb.WatchInboxRequest, stream grpc.ServerStreamingServer[notificationpb.WatchInboxResponse]) error {
	s.sessionID = request.GetSessionId()
	md, _ := metadata.FromIncomingContext(stream.Context())
	s.authorization = md.Get("authorization")
	return stream.Send(&notificationpb.WatchInboxResponse{State: &notificationpb.WatchInboxResponse_Inbox{Inbox: &notificationpb.Inbox{SessionId: s.sessionID}}})
}

func TestGeneratedNotificationWatchBindsSessionIDAndServiceToken(t *testing.T) {
	service := &generatedNotificationService{}
	server := grpc.NewServer()
	notificationpb.RegisterNotificationsServer(server, service)
	address := grpcserver.Serve(t, server)
	handler, err := facade.NewHandlerWithOptions(
		t.Context(), address, 10*time.Second,
		facade.HandlerOptions{RejectUnknownJSONFields: true}, notificationpb.RegisterNotificationsHandlerFromEndpoint,
	)
	if err != nil {
		t.Fatal(err)
	}
	tokenFile := filepath.Join(t.TempDir(), "notification-token")
	if err := os.WriteFile(tokenFile, []byte("service-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/upstream/notifications/v1/sessions/a/inbox:watch", nil)
	response := httptest.NewRecorder()
	if err := upstreamproxy.NewHandlerUpstream(handler, tokenFile).ForwardHTTP(response, request, request.Context(), "v1/sessions/a/inbox:watch", ""); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"inbox"`) {
		t.Fatalf("notification watch status=%d body=%s", response.Code, response.Body.String())
	}
	if service.sessionID != "a" {
		t.Fatalf("bound session ID = %v", service.sessionID)
	}
	if len(service.authorization) != 1 || service.authorization[0] != "Bearer service-token" {
		t.Fatalf("notification authorization = %v", service.authorization)
	}
	oldRequest := httptest.NewRequest(http.MethodPost, "/upstream/notifications/v1/messages", strings.NewReader(
		`{"destinationRef":{"namespace":"agentplane","name":"sandbox","uid":"old-uid"},"sessionId":"session","idempotencyKey":"key","body":"hello"}`,
	))
	oldRequest.Header.Set("Content-Type", "application/json")
	oldResponse := httptest.NewRecorder()
	if err := upstreamproxy.NewHandlerUpstream(handler, tokenFile).ForwardHTTP(oldResponse, oldRequest, oldRequest.Context(), "v1/messages", ""); err != nil {
		t.Fatal(err)
	}
	if oldResponse.Code != http.StatusBadRequest || service.sendCalled {
		t.Fatalf("old Notification request status=%d sent=%v body=%s", oldResponse.Code, service.sendCalled, oldResponse.Body.String())
	}
}

func (s *generatedDispatcherService) GetDispatch(ctx context.Context, request *dispatcherpb.GetDispatchRequest) (*dispatcherpb.GetDispatchResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	s.authorization = append(s.authorization, md.Get("authorization")...)
	if s.unaryStarted != nil {
		close(s.unaryStarted)
		<-ctx.Done()
		close(s.unaryCanceled)
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	return &dispatcherpb.GetDispatchResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (s *generatedDispatcherService) WatchDispatches(_ *dispatcherpb.WatchDispatchesRequest, stream grpc.ServerStreamingServer[dispatcherpb.WatchDispatchesResponse]) error {
	if s.watchStarted != nil {
		close(s.watchStarted)
		<-stream.Context().Done()
		close(s.watchCanceled)
		return status.FromContextError(stream.Context().Err()).Err()
	}
	return nil
}

func (s *generatedMetadataDispatcherService) WatchDispatches(_ *dispatcherpb.WatchDispatchesRequest, stream grpc.ServerStreamingServer[dispatcherpb.WatchDispatchesResponse]) error {
	if err := stream.SendHeader(metadata.Pairs("x-backend-header", "header-secret")); err != nil {
		return err
	}
	stream.SetTrailer(metadata.Pairs("x-backend-trailer", "trailer-secret"))
	return stream.Send(&dispatcherpb.WatchDispatchesResponse{
		Frame: &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: &dispatcherpb.Dispatch{CommandId: "streamed-command"}},
	})
}

func (s *generatedActionService) ListActionGroups(
	ctx context.Context,
	_ *actionpb.ListActionGroupsRequest,
) (*actionpb.ListActionGroupsResponse, error) {
	identity, err := peertls.PeerIdentity(ctx)
	if err != nil || identity != peertls.AppURI {
		return nil, status.Error(codes.Unauthenticated, "App SVID is required")
	}
	s.clientIdentity = append(s.clientIdentity, identity)
	md, _ := metadata.FromIncomingContext(ctx)
	authorization := md.Get("authorization")
	s.authorization = append(s.authorization, strings.Join(authorization, ","))
	s.cookie = md.Get("cookie")
	s.privateHeader = md.Get("x-private-header")
	if len(authorization) != 1 || !strings.HasPrefix(authorization[0], "Bearer ") || authorization[0] == "Bearer " {
		return nil, status.Error(codes.Unauthenticated, "operator bearer is required")
	}
	if s.started != nil {
		close(s.started)
		<-ctx.Done()
		return nil, status.Error(codes.Canceled, "request ended")
	}
	return &actionpb.ListActionGroupsResponse{ActionGroups: []*actionpb.ActionGroup{{Key: "test", Title: "Test group"}}}, nil
}

func newGeneratedActionFacade(t *testing.T, service *generatedActionService) *upstreamproxy.HandlerUpstream {
	t.Helper()
	serve := func(server *grpc.Server) {
		actionpb.RegisterActionServiceServer(server, service)
		operatoractionpb.RegisterOperatorActionServiceServer(server, &operatoractionpb.UnimplementedOperatorActionServiceServer{})
		webpushpb.RegisterWebPushServiceServer(server, &webpushpb.UnimplementedWebPushServiceServer{})
	}
	return appMTLSFacade(
		t, peertls.ActionsURI, serve,
		actionpb.RegisterActionServiceHandlerFromEndpoint,
		operatoractionpb.RegisterOperatorActionServiceHandlerFromEndpoint,
		webpushpb.RegisterWebPushServiceHandlerFromEndpoint,
	)
}

func TestGeneratedActionFacadeUsesNativeRPCAndRequestBoundBearer(t *testing.T) {
	service := &generatedActionService{}
	upstream := newGeneratedActionFacade(t, service)
	unauthenticatedRequest := httptest.NewRequest(http.MethodGet, "/upstream/actions/v1/action-groups", nil)
	unauthenticatedRequest.SetPathValue("path", "v1/action-groups")
	unauthenticatedResponse := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(unauthenticatedResponse, unauthenticatedRequest, unauthenticatedRequest.Context(), "v1/action-groups", ""); err != nil {
		t.Fatalf("ForwardHTTP() without operator bearer = %v", err)
	}
	if unauthenticatedResponse.Code == http.StatusOK {
		t.Fatal("Action facade accepted an App SVID without the operator bearer")
	}

	request := httptest.NewRequest(http.MethodGet, "/upstream/actions/v1/action-groups", nil)
	request.SetPathValue("path", "v1/action-groups")
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Cookie", "session=must-not-cross")
	request.Header.Set("X-Private-Header", "must-not-cross")
	response := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(response, request, request.Context(), "v1/action-groups", "short-lived-action-token"); err != nil {
		t.Fatalf("ForwardHTTP() = %v", err)
	}
	if response.Code != http.StatusOK {
		t.Fatalf("generated facade status = %d: %s", response.Code, response.Body.String())
	}
	var document map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &document); err != nil {
		t.Fatal(err)
	}
	groups, ok := document["actionGroups"].([]any)
	if !ok || len(groups) != 1 || groups[0].(map[string]any)["key"] != "test" {
		t.Fatalf("generated facade response = %#v", document)
	}
	if strings.Contains(response.Body.String(), "must-not-cross") {
		t.Fatalf("request-only headers escaped in response: %s", response.Body.String())
	}
	if len(service.authorization) != 2 || service.authorization[0] != "" || service.authorization[1] != "Bearer short-lived-action-token" ||
		len(service.clientIdentity) != 2 || service.clientIdentity[0] != peertls.AppURI || service.clientIdentity[1] != peertls.AppURI ||
		len(service.cookie) != 0 || len(service.privateHeader) != 0 {
		t.Fatalf("generated RPC metadata: authorization=%v peer=%v cookie=%v private=%v", service.authorization, service.clientIdentity, service.cookie, service.privateHeader)
	}
}

func TestGeneratedActionFacadeCancelsRPCWithOperatorRequest(t *testing.T) {
	service := &generatedActionService{started: make(chan struct{})}
	upstream := newGeneratedActionFacade(t, service)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	request := httptest.NewRequest(http.MethodGet, "/upstream/actions/v1/action-groups", nil).WithContext(ctx)
	request.SetPathValue("path", "v1/action-groups")
	response := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		_ = upstream.ForwardHTTP(response, request, ctx, "v1/action-groups", "short-lived-action-token")
		close(done)
	}()
	select {
	case <-service.started:
	case <-time.After(time.Second):
		t.Fatal("generated ActionService RPC did not start")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("generated ActionService RPC did not end after request cancellation")
	}
}

func TestGeneratedConnectionFacadeUsesFederatedBearer(t *testing.T) {
	service := &generatedConnectionService{}
	upstream := appMTLSFacade(t, peertls.MCPEdgeURI, func(server *grpc.Server) {
		connectionpb.RegisterConnectionServiceServer(server, service)
	}, connectionpb.RegisterConnectionServiceHandlerFromEndpoint)
	request := httptest.NewRequest(http.MethodGet, "/upstream/connections/v1/operator/connections", nil)
	response := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(response, request, request.Context(), "v1/operator/connections", "connection-token"); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || len(service.authorization) != 1 || service.authorization[0] != "Bearer connection-token" ||
		len(service.clientIdentity) != 1 || service.clientIdentity[0] != peertls.AppURI {
		t.Fatalf("Connection facade status=%d authorization=%v peer=%v body=%s", response.Code, service.authorization, service.clientIdentity, response.Body.String())
	}
}

func TestGeneratedDispatcherFacadeReadsRotatingServiceToken(t *testing.T) {
	service := &generatedDispatcherService{}
	server := grpc.NewServer()
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	address := grpcserver.Serve(t, server)
	handler, err := facade.NewHandler(t.Context(), address, 10*time.Second, dispatcherpb.RegisterSessionDispatcherHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	tokenFile := filepath.Join(t.TempDir(), "dispatcher-token")
	if err := os.WriteFile(tokenFile, []byte("first-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	upstream := upstreamproxy.NewHandlerUpstream(handler, tokenFile)
	for _, token := range []string{"first-token", "rotated-token"} {
		if err := os.WriteFile(tokenFile, []byte(token+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		request := httptest.NewRequest(http.MethodGet, "/upstream/dispatcher/v1/dispatches/command-a", nil)
		response := httptest.NewRecorder()
		if err := upstream.ForwardHTTP(response, request, request.Context(), "v1/dispatches/command-a", ""); err != nil {
			t.Fatal(err)
		}
		if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"commandId":"command-a"`) {
			t.Fatalf("Session Dispatcher facade status=%d body=%s", response.Code, response.Body.String())
		}
	}
	if len(service.authorization) != 2 || service.authorization[0] != "Bearer first-token" || service.authorization[1] != "Bearer rotated-token" {
		t.Fatalf("Session Dispatcher authorization metadata = %v", service.authorization)
	}
}

func TestGeneratedDispatcherFacadeBoundsUnaryButNotWatchStream(t *testing.T) {
	const unaryTimeout = 50 * time.Millisecond
	service := &generatedDispatcherService{
		unaryStarted:  make(chan struct{}),
		unaryCanceled: make(chan struct{}),
		watchStarted:  make(chan struct{}),
		watchCanceled: make(chan struct{}),
	}
	upstream := newGeneratedDispatcherFacade(t, service, unaryTimeout)

	unaryRequest := httptest.NewRequest(http.MethodGet, "/upstream/dispatcher/v1/dispatches/command-a", nil)
	unaryResponse := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(unaryResponse, unaryRequest, unaryRequest.Context(), "v1/dispatches/command-a", ""); err != nil {
		t.Fatal(err)
	}
	if unaryResponse.Code != http.StatusGatewayTimeout {
		t.Fatalf("unary facade status = %d, want %d: %s", unaryResponse.Code, http.StatusGatewayTimeout, unaryResponse.Body.String())
	}
	select {
	case <-service.unaryCanceled:
	case <-time.After(time.Second):
		t.Fatal("configured unary timeout did not cancel the generated RPC")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	watchRequest := httptest.NewRequest(http.MethodGet, "/upstream/dispatcher/v1/dispatches:watch", nil).WithContext(ctx)
	watchResponse := httptest.NewRecorder()
	watchDone := make(chan struct{})
	go func() {
		_ = upstream.ForwardHTTP(watchResponse, watchRequest, ctx, "v1/dispatches:watch", "")
		close(watchDone)
	}()
	select {
	case <-service.watchStarted:
	case <-time.After(time.Second):
		t.Fatal("server-streaming generated RPC did not start")
	}
	select {
	case <-service.watchCanceled:
		t.Fatal("unary timeout incorrectly bounded the generated stream")
	case <-time.After(3 * unaryTimeout):
	}
	cancel()
	select {
	case <-service.watchCanceled:
	case <-time.After(time.Second):
		t.Fatal("HTTP request cancellation did not cancel the generated stream")
	}
	select {
	case <-watchDone:
	case <-time.After(time.Second):
		t.Fatal("generated stream handler did not finish after request cancellation")
	}
}

func TestGeneratedDispatcherFacadeFiltersResponseHeadersAndPreservesStreaming(t *testing.T) {
	handler := newGeneratedDispatcherHandler(t, &generatedMetadataDispatcherService{}, 10*time.Second)
	rawResponse := httptest.NewRecorder()
	handler.ServeHTTP(rawResponse, httptest.NewRequest(http.MethodGet, "/v1/dispatches:watch", nil))
	if rawResponse.Header().Get("Grpc-Metadata-X-Backend-Header") != "header-secret" {
		t.Fatalf("generated handler did not expose backend metadata for the boundary test: %v", rawResponse.Header())
	}

	upstream := upstreamproxy.NewHandlerUpstream(handler, "")
	response := httptest.NewRecorder()
	trackedResponse := &flushCountingResponseWriter{ResponseRecorder: response}
	request := httptest.NewRequest(http.MethodGet, "/upstream/dispatcher/v1/dispatches:watch", nil)
	if err := upstream.ForwardHTTP(trackedResponse, request, request.Context(), "v1/dispatches:watch", ""); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "streamed-command") {
		t.Fatalf("generated stream response = %d %q", response.Code, response.Body.String())
	}
	if got := response.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf("Content-Type = %q, want application/json", got)
	}
	if got := response.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("Cache-Control = %q, want no-store", got)
	}
	if got := response.Header().Get("X-Accel-Buffering"); got != "no" {
		t.Fatalf("X-Accel-Buffering = %q, want no", got)
	}
	if trackedResponse.flushes == 0 {
		t.Fatal("generated stream did not flush through the response wrapper")
	}
	result := response.Result()
	for _, headers := range []http.Header{result.Header, result.Trailer} {
		for name, values := range headers {
			lowerName := strings.ToLower(name)
			if strings.HasPrefix(lowerName, "grpc-metadata-") || strings.HasPrefix(lowerName, "grpc-trailer-") ||
				strings.Contains(strings.Join(values, " "), "header-secret") || strings.Contains(strings.Join(values, " "), "trailer-secret") {
				t.Fatalf("gRPC response metadata leaked as %s: %v", name, values)
			}
		}
	}
}

type flushCountingResponseWriter struct {
	*httptest.ResponseRecorder
	flushes int
}

func (w *flushCountingResponseWriter) Flush() {
	w.flushes++
	w.ResponseRecorder.Flush()
}

func newGeneratedDispatcherFacade(t *testing.T, service *generatedDispatcherService, unaryTimeout time.Duration) *upstreamproxy.HandlerUpstream {
	t.Helper()
	return upstreamproxy.NewHandlerUpstream(newGeneratedDispatcherHandler(t, service, unaryTimeout), "")
}

func newGeneratedDispatcherHandler(t *testing.T, service dispatcherpb.SessionDispatcherServer, unaryTimeout time.Duration) http.Handler {
	t.Helper()
	server := grpc.NewServer()
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	address := grpcserver.Serve(t, server)
	handler, err := facade.NewHandler(t.Context(), address, unaryTimeout, dispatcherpb.RegisterSessionDispatcherHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	return handler
}

// appMTLSFacade serves the gRPC services registered by serve under serverURI's SVID, admitting only the
// App, and returns the generated HTTP facade the App uses to reach them with its own SVID.
func appMTLSFacade(t *testing.T, serverURI string, serve func(*grpc.Server), registrations ...facade.Registration) *upstreamproxy.HandlerUpstream {
	t.Helper()
	ca := peertlstesting.NewCA(t)
	serverCredentials, err := ca.Issue(t, serverURI).ServerCredentials(peertls.AppURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(serverCredentials))
	serve(server)
	address := grpcserver.Serve(t, server)
	appSVID := ca.Issue(t, peertls.AppURI)
	handler, err := facade.NewHandlerWithOptions(
		t.Context(), address, 10*time.Second,
		facade.HandlerOptions{TLS: &peertls.ClientTLS{
			CAFile: ca.RootFile, ServerURI: serverURI,
			CertFile: appSVID.CertFile, KeyFile: appSVID.KeyFile, ClientURI: peertls.AppURI,
		}},
		registrations...,
	)
	if err != nil {
		t.Fatal(err)
	}
	return upstreamproxy.NewHandlerUpstream(handler, "")
}

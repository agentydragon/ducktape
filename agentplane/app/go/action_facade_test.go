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

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	operatoractionpb "github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	webpushpb "github.com/agentydragon/ducktape/agentplane/action_service/web_push_servicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/proxy"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	connectionpb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	notificationpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type generatedActionService struct {
	actionpb.UnimplementedActionServiceServer

	authorization []string
	cookie        []string
	privateHeader []string
	started       chan struct{}
}

type generatedConnectionService struct {
	connectionpb.UnimplementedConnectionServiceServer
	authorization []string
}

func (s *generatedConnectionService) ListConnections(ctx context.Context, _ *connectionpb.ListConnectionsRequest) (*connectionpb.ListConnectionsResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	s.authorization = append(s.authorization, md.Get("authorization")...)
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
	notificationpb.UnimplementedNotificationsOperatorServer
	sessionIDs    []string
	authorization []string
}

func (s *generatedNotificationService) WatchSessionNotices(request *notificationpb.WatchSessionNoticesRequest, stream grpc.ServerStreamingServer[notificationpb.WatchSessionNoticesResponse]) error {
	s.sessionIDs = request.GetSessionIds()
	md, _ := metadata.FromIncomingContext(stream.Context())
	s.authorization = md.Get("authorization")
	return stream.Send(&notificationpb.WatchSessionNoticesResponse{Frame: &notificationpb.WatchSessionNoticesResponse_Snapshot{Snapshot: &notificationpb.SessionNoticeStates{}}})
}

func TestGeneratedNotificationWatchBindsRepeatedSessionIDsAndServiceToken(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	service := &generatedNotificationService{}
	server := grpc.NewServer()
	notificationpb.RegisterNotificationsOperatorServer(server, service)
	serveDone := make(chan struct{})
	go func() {
		defer close(serveDone)
		_ = server.Serve(listener)
	}()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		<-serveDone
	})
	handler, err := facade.NewHandler(t.Context(), listener.Addr().String(), 10*time.Second, notificationpb.RegisterNotificationsOperatorHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	tokenFile := filepath.Join(t.TempDir(), "notification-token")
	if err := os.WriteFile(tokenFile, []byte("service-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/upstream/notifications/v1/operator/notices:watch?sessionIds=a&sessionIds=b", nil)
	response := httptest.NewRecorder()
	if err := proxy.NewHandlerUpstream(handler, tokenFile).ForwardHTTP(response, request, request.Context(), "v1/operator/notices:watch", ""); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"snapshot"`) {
		t.Fatalf("notification watch status=%d body=%s", response.Code, response.Body.String())
	}
	if len(service.sessionIDs) != 2 || service.sessionIDs[0] != "a" || service.sessionIDs[1] != "b" {
		t.Fatalf("bound session IDs = %v", service.sessionIDs)
	}
	if len(service.authorization) != 1 || service.authorization[0] != "Bearer service-token" {
		t.Fatalf("notification authorization = %v", service.authorization)
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
	md, _ := metadata.FromIncomingContext(ctx)
	s.authorization = md.Get("authorization")
	s.cookie = md.Get("cookie")
	s.privateHeader = md.Get("x-private-header")
	if s.started != nil {
		close(s.started)
		<-ctx.Done()
		return nil, status.Error(codes.Canceled, "request ended")
	}
	return &actionpb.ListActionGroupsResponse{ActionGroups: []*actionpb.ActionGroup{{Key: "test", Title: "Test group"}}}, nil
}

func newGeneratedActionFacade(t *testing.T, service *generatedActionService) *proxy.HandlerUpstream {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	actionpb.RegisterActionServiceServer(server, service)
	operatoractionpb.RegisterOperatorActionServiceServer(server, &operatoractionpb.UnimplementedOperatorActionServiceServer{})
	webpushpb.RegisterWebPushServiceServer(server, &webpushpb.UnimplementedWebPushServiceServer{})
	serveDone := make(chan struct{})
	go func() {
		defer close(serveDone)
		_ = server.Serve(listener)
	}()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		<-serveDone
	})

	handler, err := facade.NewHandler(
		t.Context(), listener.Addr().String(), 10*time.Second,
		actionpb.RegisterActionServiceHandlerFromEndpoint,
		operatoractionpb.RegisterOperatorActionServiceHandlerFromEndpoint,
		webpushpb.RegisterWebPushServiceHandlerFromEndpoint,
	)
	if err != nil {
		t.Fatal(err)
	}
	return proxy.NewHandlerUpstream(handler, "")
}

func TestGeneratedActionFacadeUsesNativeRPCAndRequestBoundBearer(t *testing.T) {
	service := &generatedActionService{}
	upstream := newGeneratedActionFacade(t, service)
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
	if len(service.authorization) != 1 || service.authorization[0] != "Bearer short-lived-action-token" ||
		len(service.cookie) != 0 || len(service.privateHeader) != 0 {
		t.Fatalf("generated RPC metadata: authorization=%v cookie=%v private=%v", service.authorization, service.cookie, service.privateHeader)
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
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	service := &generatedConnectionService{}
	server := grpc.NewServer()
	connectionpb.RegisterConnectionServiceServer(server, service)
	serveDone := make(chan struct{})
	go func() {
		defer close(serveDone)
		_ = server.Serve(listener)
	}()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		<-serveDone
	})
	handler, err := facade.NewHandler(t.Context(), listener.Addr().String(), 10*time.Second, connectionpb.RegisterConnectionServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	upstream := proxy.NewHandlerUpstream(handler, "")
	request := httptest.NewRequest(http.MethodGet, "/upstream/connections/v1/operator/connections", nil)
	response := httptest.NewRecorder()
	if err := upstream.ForwardHTTP(response, request, request.Context(), "v1/operator/connections", "connection-token"); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || len(service.authorization) != 1 || service.authorization[0] != "Bearer connection-token" {
		t.Fatalf("Connection facade status=%d authorization=%v body=%s", response.Code, service.authorization, response.Body.String())
	}
}

func TestGeneratedDispatcherFacadeReadsRotatingServiceToken(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	service := &generatedDispatcherService{}
	server := grpc.NewServer()
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	serveDone := make(chan struct{})
	go func() {
		defer close(serveDone)
		_ = server.Serve(listener)
	}()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		<-serveDone
	})
	handler, err := facade.NewHandler(t.Context(), listener.Addr().String(), 10*time.Second, dispatcherpb.RegisterSessionDispatcherHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	tokenFile := filepath.Join(t.TempDir(), "dispatcher-token")
	if err := os.WriteFile(tokenFile, []byte("first-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	upstream := proxy.NewHandlerUpstream(handler, tokenFile)
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

	upstream := proxy.NewHandlerUpstream(handler, "")
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

func newGeneratedDispatcherFacade(t *testing.T, service *generatedDispatcherService, unaryTimeout time.Duration) *proxy.HandlerUpstream {
	t.Helper()
	return proxy.NewHandlerUpstream(newGeneratedDispatcherHandler(t, service, unaryTimeout), "")
}

func newGeneratedDispatcherHandler(t *testing.T, service dispatcherpb.SessionDispatcherServer, unaryTimeout time.Duration) http.Handler {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	serveDone := make(chan struct{})
	go func() {
		defer close(serveDone)
		_ = server.Serve(listener)
	}()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		<-serveDone
	})
	handler, err := facade.NewHandler(t.Context(), listener.Addr().String(), unaryTimeout, dispatcherpb.RegisterSessionDispatcherHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	return handler
}

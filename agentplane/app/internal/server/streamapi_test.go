package server

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
	upstreamproxy "github.com/agentydragon/ducktape/agentplane/app/internal/upstream"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/encoding/protojson"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestWriteSSEPreservesJSONData(t *testing.T) {
	response := httptest.NewRecorder()
	if err := startSSE(response); err != nil {
		t.Fatal(err)
	}
	if err := writeSSE(response, "event", map[string]any{"kind": "tool", "text": "hello"}); err != nil {
		t.Fatal(err)
	}
	if response.Code != 200 {
		t.Fatalf("status = %d, want 200", response.Code)
	}
	if got := response.Header().Get("Content-Type"); got != "text/event-stream; charset=utf-8" {
		t.Fatalf("Content-Type = %q", got)
	}
	if got := response.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("Cache-Control = %q", got)
	}
	for _, want := range []string{"event: event\n", `data: {"kind":"tool","text":"hello"}`, "\n\n"} {
		if !strings.Contains(response.Body.String(), want) {
			t.Fatalf("SSE frame %q does not contain %q", response.Body.String(), want)
		}
	}
}

type acceptedTokenReviewer struct{}

func (acceptedTokenReviewer) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{"agentplane"},
		User:          authenticationv1.UserInfo{Username: "system:serviceaccount:tools:runner"},
	}}, nil
}

func newStreamTestAuth(t *testing.T) *auth.Auth {
	t.Helper()
	authentication, err := auth.New(auth.Config{
		TokenReviews: acceptedTokenReviewer{}, TokenAudience: "agentplane",
		TokenSubjects: []string{"system:serviceaccount:tools:runner"},
	})
	if err != nil {
		t.Fatal(err)
	}
	return authentication
}

func TestOperatorStreamsAndProxyRoutesRejectWorkloadTokens(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication)
	registerStreamingRoutes(server, streamingDependencies{})
	ids := uuid.NewString()
	for _, path := range []string{
		"/upstream/notifications/v1/sessions/" + ids + "/inbox:watch",
		"/sandboxes/demo/console/stream",
		"/upstream/dispatcher/v1/items",
		"/upstream/actions/v1/items",
		"/upstream/connections/v1/items",
		"/upstream/egress/v1/credentials:watch",
		"/upstream/egress/v1/decisions:watch",
	} {
		t.Run(path, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodGet, path, nil)
			request.Header.Set("Authorization", "Bearer accepted")
			response := httptest.NewRecorder()
			server.Handler(authentication).ServeHTTP(response, request)
			if response.Code != http.StatusForbidden {
				t.Fatalf("workload-token access to operator route = %d %s, want 403", response.Code, response.Body.String())
			}
		})
	}
	for _, route := range []struct{ method, path string }{
		{http.MethodPost, "/upstream/policy/v1/sandboxes/tools/demo/policy-bindings"},
		{http.MethodDelete, "/upstream/policy/v1/sandboxes/tools/demo/policy-bindings/grant?sandboxUid=sandbox-uid&bindingUid=binding-uid"},
	} {
		t.Run(route.method+" "+route.path, func(t *testing.T) {
			request := httptest.NewRequest(route.method, route.path, nil)
			request.Header.Set("Authorization", "Bearer accepted")
			response := httptest.NewRecorder()
			server.Handler(authentication).ServeHTTP(response, request)
			if response.Code != http.StatusForbidden {
				t.Fatalf("workload-token access to operator policy mutation = %d %s, want 403", response.Code, response.Body.String())
			}
		})
	}
}

func TestHistoryFacadeRequiresAnAuthenticatedCaller(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication)
	var gotPath, gotBearer string
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, _ *http.Request, _ context.Context, path, bearer string) error {
		gotPath, gotBearer = path, bearer
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, err := io.WriteString(w, `{"observations":[]}`)
		return err
	}}
	registerStreamingRoutes(server, streamingDependencies{HistoryHTTP: upstream})

	path := "/upstream/history/v1/sessions/9b6d568a-2253-43d7-a7d4-190e98359594/observations"
	unauthenticated := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodGet, path, nil))
	if unauthenticated.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated History facade status = %d, want %d", unauthenticated.Code, http.StatusUnauthorized)
	}

	request := httptest.NewRequest(http.MethodGet, path+"?limit=30", nil)
	request.Header.Set("Authorization", "Bearer accepted")
	response := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusOK || response.Body.String() != `{"observations":[]}` {
		t.Fatalf("authenticated History facade response = %d %q", response.Code, response.Body.String())
	}
	if gotPath != "v1/sessions/9b6d568a-2253-43d7-a7d4-190e98359594/observations" || gotBearer != "" {
		t.Fatalf("History upstream path/bearer = %q/%q", gotPath, gotBearer)
	}
}

type typedHistoryServer struct {
	historypb.UnimplementedHistoryServiceServer
	requests chan *historypb.ReadThreadEntitiesRequest
}

func (s *typedHistoryServer) ReadThreadEntities(_ context.Context, request *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error) {
	s.requests <- request
	return &historypb.ReadThreadEntitiesResponse{
		ProjectionEpoch: "history-v1", ThroughCursor: 1<<53 + 7,
		Entities: []*historypb.ThreadEntity{{
			SessionId: request.SessionId, EntityId: "tool", EntityIndex: 2,
			Entity: &historypb.ThreadEntity_Item{Item: &historypb.ThreadItem{
				Kind:       eventpb.ItemKind_ITEM_KIND_TOOL_CALL,
				Completion: &historypb.ThreadItem_Tool{Tool: &historypb.ThreadItemToolCompletion{Succeeded: false}},
			}},
		}},
	}, nil
}

func TestHistoryEntityGeneratedFacade(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	grpcServer := grpc.NewServer()
	servicer := &typedHistoryServer{requests: make(chan *historypb.ReadThreadEntitiesRequest, 1)}
	historypb.RegisterHistoryServiceServer(grpcServer, servicer)
	go func() { _ = grpcServer.Serve(listener) }()
	t.Cleanup(grpcServer.Stop)
	generated, err := facade.NewHandler(t.Context(), listener.Addr().String(), time.Second, historypb.RegisterHistoryServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	authentication := newStreamTestAuth(t)
	app := newHTTPServer(authentication)
	registerStreamingRoutes(app, streamingDependencies{HistoryHTTP: upstreamproxy.NewHandlerUpstream(generated, "")})
	const id = "9b6d568a-2253-43d7-a7d4-190e98359594"
	body, err := protojson.Marshal(&historypb.ReadThreadEntitiesRequest{Window: &historypb.ReadThreadEntitiesRequest_Range{
		Range: &historypb.EntityRange{StartEntityIndex: 2, Limit: 1},
	}})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/upstream/history/v1/sessions/"+id+"/entities", bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer accepted")
	response := httptest.NewRecorder()
	app.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("entity facade status = %d: %s", response.Code, response.Body.String())
	}
	got := &historypb.ReadThreadEntitiesResponse{}
	if err := protojson.Unmarshal(response.Body.Bytes(), got); err != nil {
		t.Fatal(err)
	}
	if got.ThroughCursor != 1<<53+7 || len(got.Entities) != 1 || got.Entities[0].GetItem().GetTool() == nil || got.Entities[0].GetItem().GetTool().Succeeded {
		t.Fatalf("entity facade lost protobuf data: %v", got)
	}
	received := <-servicer.requests
	if received.SessionId != id || received.GetRange().GetStartEntityIndex() != 2 || received.GetRange().GetLimit() != 1 {
		t.Fatalf("entity request = %v", received)
	}
	body, err = protojson.Marshal(&historypb.ReadThreadEntitiesRequest{Window: &historypb.ReadThreadEntitiesRequest_Keys{
		Keys: &historypb.EntityKeys{Keys: []*historypb.EntityKey{{EntityKind: historypb.ThreadEntityKind_THREAD_ENTITY_KIND_ITEM, EntityId: "tool"}}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	request = httptest.NewRequest(http.MethodPost, "/upstream/history/v1/sessions/"+id+"/entities", bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer accepted")
	response = httptest.NewRecorder()
	app.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("keyed entity facade status = %d: %s", response.Code, response.Body.String())
	}
	received = <-servicer.requests
	if len(received.GetKeys().GetKeys()) != 1 || received.GetKeys().GetKeys()[0].GetEntityKind() != historypb.ThreadEntityKind_THREAD_ENTITY_KIND_ITEM {
		t.Fatalf("keyed entity request = %v", received)
	}
}

func TestDispatcherFacadeRejectsWorkloadCommandPOSTs(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication)
	forwarded := 0
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, _ *http.Request, _ context.Context, _, _ string) error {
		forwarded++
		w.WriteHeader(http.StatusAccepted)
		return nil
	}}
	registerStreamingRoutes(server, streamingDependencies{DispatcherHTTP: upstream})
	const prefix = "/upstream/dispatcher/v1/sessions/9b6d568a-2253-43d7-a7d4-190e98359594"
	const body = `{"commandId":"stable","sandbox":"demo","sandboxUid":"uid-1"}`

	unauthenticated := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodPost, prefix+":submitInput", strings.NewReader(body)))
	if unauthenticated.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous command status = %d, want 401", unauthenticated.Code)
	}
	for _, method := range []string{"submitInput", "changeModel", "changeReasoningEffort", "interruptTurn"} {
		request := httptest.NewRequest(http.MethodPost, prefix+":"+method, strings.NewReader(body))
		request.Header.Set("Authorization", "Bearer accepted")
		response := httptest.NewRecorder()
		server.Handler(authentication).ServeHTTP(response, request)
		if response.Code != http.StatusForbidden {
			t.Fatalf("workload-token %s status = %d %s, want 403", method, response.Code, response.Body.String())
		}
	}
	openingRequest := httptest.NewRequest(http.MethodGet, prefix+"/opening", nil)
	openingRequest.Header.Set("Authorization", "Bearer accepted")
	openingResponse := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(openingResponse, openingRequest)
	if openingResponse.Code != http.StatusForbidden {
		t.Fatalf("workload-token opening read status = %d %s, want 403", openingResponse.Code, openingResponse.Body.String())
	}
	if forwarded != 0 {
		t.Fatalf("workload request reached Dispatcher %d times", forwarded)
	}
}

func TestConsoleRelayStreamsBase64OutputAndClosesOnCancellation(t *testing.T) {
	reader, writer := io.Pipe()
	response := newStreamingRecorder()
	if err := startSSE(response); err != nil {
		t.Fatal(err)
	}
	routes := &streamingRoutes{}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		routes.relayConsole(response, ctx, reader)
		close(done)
	}()
	if _, err := writer.Write([]byte("terminal output")); err != nil {
		t.Fatal(err)
	}
	waitForStreamText(t, response, base64.StdEncoding.EncodeToString([]byte("terminal output")))
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("console relay did not cancel a blocked read")
	}
	_ = writer.Close()
}

type streamTestUpstream struct {
	forward func(http.ResponseWriter, *http.Request, context.Context, string, string) error
}

func (f *streamTestUpstream) ForwardHTTP(w http.ResponseWriter, request *http.Request, ctx context.Context, path, bearer string) error {
	return f.forward(w, request, ctx, path, bearer)
}

func TestUpstreamRouteForwardersPreserveFacadeAndDispatcherResponses(t *testing.T) {
	routes := &streamingRoutes{}
	var bodyText string
	var gotMethod, gotPath, gotQuery, gotBearer string
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, request *http.Request, _ context.Context, path, bearer string) error {
		gotMethod, gotPath, gotQuery, gotBearer = request.Method, path, request.URL.RawQuery, bearer
		requestBody, err := io.ReadAll(request.Body)
		if err != nil {
			return err
		}
		bodyText = string(requestBody)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_, err = io.WriteString(w, `{"ok":true}`)
		return err
	}}
	request := httptest.NewRequest(http.MethodPost, "/upstream/actions/v1/items?x=1", strings.NewReader("streamed request"))
	request.SetPathValue("path", "v1/items")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	request.Header.Set("User-Agent", "stream-test")
	request.Header.Set("Cookie", "must-not-forward")
	response := httptest.NewRecorder()
	routes.forwardFacade(response, request, context.Background(), upstream, "v1/items", "federated-token")
	if response.Code != http.StatusAccepted || response.Body.String() != `{"ok":true}` {
		t.Fatalf("action facade response = %d %q", response.Code, response.Body.String())
	}
	if gotMethod != http.MethodPost || gotPath != "v1/items" || gotQuery != "x=1" || gotBearer != "federated-token" || bodyText != "streamed request" {
		t.Fatalf("facade request = %s %s?%s bearer=%q body=%q", gotMethod, gotPath, gotQuery, gotBearer, bodyText)
	}
	if response.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("facade response headers = %#v", response.Header())
	}

	failed := &streamTestUpstream{forward: func(http.ResponseWriter, *http.Request, context.Context, string, string) error {
		return context.DeadlineExceeded
	}}
	failedResponse := httptest.NewRecorder()
	routes.forwardFacade(failedResponse, request, context.Background(), failed, "v1/items", "token")
	var failedBody struct {
		Detail struct {
			Method    string `json:"method"`
			URL       string `json:"url"`
			ErrorType string `json:"error_type"`
		} `json:"detail"`
	}
	if err := json.Unmarshal(failedResponse.Body.Bytes(), &failedBody); err != nil {
		t.Fatal(err)
	}
	if failedResponse.Code != http.StatusGatewayTimeout || failedBody.Detail.Method != http.MethodPost ||
		failedBody.Detail.URL != request.URL.Path || failedBody.Detail.ErrorType == "" {
		t.Fatalf("facade forwarding error = %d %s", failedResponse.Code, failedResponse.Body.String())
	}

	var dispatcherPath, dispatcherQuery string
	dispatcher := &streamTestUpstream{forward: func(w http.ResponseWriter, request *http.Request, _ context.Context, path, bearer string) error {
		dispatcherPath, dispatcherQuery = path, request.URL.RawQuery
		if bearer != "" {
			t.Errorf("dispatcher forward bearer = %q", bearer)
		}
		w.WriteHeader(http.StatusPartialContent)
		_, err := io.WriteString(w, "dispatcher bytes")
		return err
	}}
	routes.deps.DispatcherHTTP = dispatcher
	dispatcherRequest := httptest.NewRequest(http.MethodGet, "/upstream/dispatcher/v1/sessions?cursor=3", nil)
	dispatcherRequest.SetPathValue("path", "v1/sessions")
	dispatcherResponse := httptest.NewRecorder()
	routes.forwardDispatcher(dispatcherResponse, dispatcherRequest, context.Background())
	if dispatcherPath != "v1/sessions" || dispatcherQuery != "cursor=3" || dispatcherResponse.Code != http.StatusPartialContent || dispatcherResponse.Body.String() != "dispatcher bytes" {
		t.Fatalf("dispatcher forwarding = %q?%q -> %d %q", dispatcherPath, dispatcherQuery, dispatcherResponse.Code, dispatcherResponse.Body.String())
	}
}

func TestUpstreamFacadeForwarderPassesCancellation(t *testing.T) {
	entered := make(chan struct{})
	upstream := &streamTestUpstream{forward: func(_ http.ResponseWriter, _ *http.Request, ctx context.Context, _, _ string) error {
		close(entered)
		<-ctx.Done()
		return ctx.Err()
	}}
	ctx, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodGet, "/upstream/actions/v1/items", nil)
	response := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		(&streamingRoutes{}).forwardFacade(response, request, ctx, upstream, "v1/items", "token")
		close(done)
	}()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("upstream was not called")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("upstream forwarder did not return after request cancellation")
	}
}

type streamingRecorder struct {
	mu      sync.Mutex
	header  http.Header
	body    bytes.Buffer
	status  int
	written chan struct{}
}

func newStreamingRecorder() *streamingRecorder {
	return &streamingRecorder{header: make(http.Header), written: make(chan struct{}, 16)}
}

func (r *streamingRecorder) Header() http.Header { return r.header }

func (r *streamingRecorder) WriteHeader(status int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.status == 0 {
		r.status = status
	}
	r.signal()
}

func (r *streamingRecorder) Write(data []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.status == 0 {
		r.status = http.StatusOK
	}
	n, err := r.body.Write(data)
	r.signal()
	return n, err
}

func (r *streamingRecorder) Flush() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.signal()
}

func (r *streamingRecorder) signal() {
	select {
	case r.written <- struct{}{}:
	default:
	}
}

func (r *streamingRecorder) String() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.body.String()
}

func waitForStreamText(t *testing.T, response *streamingRecorder, text string) {
	t.Helper()
	deadline := time.After(2 * time.Second)
	for {
		if strings.Contains(response.String(), text) {
			return
		}
		select {
		case <-response.written:
		case <-deadline:
			t.Fatalf("stream did not contain %q; body: %s", text, response.String())
		}
	}
}

func TestEnvironmentFacadeRequiresAppCallerAndForwardsManagementAndWatch(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication)
	var gotPath, gotMethod, gotQuery string
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, request *http.Request, _ context.Context, path, bearer string) error {
		gotPath, gotMethod, gotQuery = path, request.Method, request.URL.RawQuery
		if bearer != "" {
			t.Fatalf("unexpected forwarded bearer: %q", bearer)
		}
		w.WriteHeader(http.StatusOK)
		return nil
	}}
	registerStreamingRoutes(server, streamingDependencies{EnvironmentHTTP: upstream})
	path := "/upstream/environments/v1/environments:watch"
	unauthenticated := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodGet, path, nil))
	if unauthenticated.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous watch = %d", unauthenticated.Code)
	}
	request := httptest.NewRequest(http.MethodGet, path, nil)
	request.Header.Set("Authorization", "Bearer accepted")
	response := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusOK || gotPath != "v1/environments:watch" {
		t.Fatalf("watch = %d, path = %q", response.Code, gotPath)
	}
	for _, methodPath := range []struct{ method, path, wantPath, wantQuery string }{
		{http.MethodGet, "/upstream/environments/v1/environment-templates", "v1/environment-templates", ""},
		{http.MethodGet, "/upstream/environments/v1/environments/test-envs/workspace:identity-record?target.uid=selected-uid", "v1/environments/test-envs/workspace:identity-record", "target.uid=selected-uid"},
		{http.MethodPost, "/upstream/environments/v1/environments", "v1/environments", ""},
		{http.MethodDelete, "/upstream/environments/v1/environments/test-envs/workspace?target.uid=selected-uid", "v1/environments/test-envs/workspace", "target.uid=selected-uid"},
		{http.MethodPatch, "/upstream/environments/v1/environments/test-envs/workspace:idle-suspend", "v1/environments/test-envs/workspace:idle-suspend", ""},
		{http.MethodPatch, "/upstream/environments/v1/environments/test-envs/workspace:identity", "v1/environments/test-envs/workspace:identity", ""},
	} {
		request := httptest.NewRequest(methodPath.method, methodPath.path, nil)
		request.Header.Set("Authorization", "Bearer accepted")
		response := httptest.NewRecorder()
		server.Handler(authentication).ServeHTTP(response, request)
		if response.Code != http.StatusOK || gotPath != methodPath.wantPath || gotMethod != methodPath.method || gotQuery != methodPath.wantQuery {
			t.Fatalf("%s %s = %d, forwarded %s %s?%s", methodPath.method, methodPath.path, response.Code, gotMethod, gotPath, gotQuery)
		}
	}
}

func TestEnvironmentGetRequiresAppCallerAndForwardsOnlyTarget(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication)
	var gotMethod, gotPath, gotQuery string
	var calls int
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, request *http.Request, _ context.Context, path, bearer string) error {
		calls++
		gotMethod, gotPath, gotQuery = request.Method, path, request.URL.RawQuery
		if bearer != "" {
			t.Fatalf("unexpected forwarded bearer: %q", bearer)
		}
		w.WriteHeader(http.StatusOK)
		return nil
	}}
	registerStreamingRoutes(server, streamingDependencies{EnvironmentHTTP: upstream})
	path := "/upstream/environments/v1/environments/team-a/sandbox-1?target.uid=22222222-2222-4222-8222-222222222222"
	unauthenticated := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodGet, path, nil))
	if unauthenticated.Code != http.StatusUnauthorized || calls != 0 {
		t.Fatalf("anonymous get = %d, upstream calls = %d", unauthenticated.Code, calls)
	}

	request := httptest.NewRequest(http.MethodGet, path, nil)
	request.Header.Set("Authorization", "Bearer accepted")
	response := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusOK || calls != 1 {
		t.Fatalf("authenticated get = %d, upstream calls = %d", response.Code, calls)
	}
	if gotMethod != http.MethodGet || gotPath != "v1/environments/team-a/sandbox-1" ||
		gotQuery != "target.uid=22222222-2222-4222-8222-222222222222" {
		t.Fatalf("forwarded get = %s %s?%s", gotMethod, gotPath, gotQuery)
	}
}

func TestEnvironmentGeneratedFacadeRejectsUnknownAndDispatcherRoutes(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication)
	generated, err := facade.NewHandler(t.Context(), "127.0.0.1:1", time.Second, environmentpb.RegisterEnvironmentServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	registerStreamingRoutes(server, streamingDependencies{EnvironmentHTTP: upstreamproxy.NewHandlerUpstream(generated, "")})
	handler := server.Handler(authentication)
	for _, test := range []struct {
		method, path string
		status       int
	}{
		{http.MethodGet, "/upstream/environments/v1/environments/team-a/sandbox-1/extra", http.StatusNotFound},
		{http.MethodPost, "/upstream/environments/v1/environments/team-a/sandbox-1:unknown", http.StatusNotImplemented},
		{http.MethodPatch, "/upstream/environments/v1/environments/team-a/sandbox-1:resume", http.StatusNotImplemented},
		{http.MethodGet, "/upstream/environments/v1/dispatches:watch", http.StatusNotFound},
	} {
		request := httptest.NewRequest(test.method, test.path, nil)
		request.Header.Set("Authorization", "Bearer accepted")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != test.status {
			t.Errorf("%s %s = %d, want %d", test.method, test.path, response.Code, test.status)
		}
	}
}

func TestEnvironmentGetCancelsUpstreamWithRequest(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication)
	started := make(chan context.Context, 1)
	canceled := make(chan error, 1)
	upstream := &streamTestUpstream{forward: func(_ http.ResponseWriter, _ *http.Request, ctx context.Context, _, _ string) error {
		started <- ctx
		<-ctx.Done()
		canceled <- ctx.Err()
		return ctx.Err()
	}}
	registerStreamingRoutes(server, streamingDependencies{EnvironmentHTTP: upstream})
	ctx, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodGet, "/upstream/environments/v1/environments/team-a/sandbox-1?target.uid=uid", nil).WithContext(ctx)
	request.Header.Set("Authorization", "Bearer accepted")
	response := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		server.Handler(authentication).ServeHTTP(response, request)
		close(done)
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		cancel()
		t.Fatal("EnvironmentService Get did not reach upstream")
	}
	cancel()
	select {
	case err := <-canceled:
		if err != context.Canceled {
			t.Fatalf("upstream context error = %v, want context.Canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("request cancellation did not reach EnvironmentService Get")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("EnvironmentService Get did not finish after request cancellation")
	}
}

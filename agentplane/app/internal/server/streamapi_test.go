package server

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	streamhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/streams"
	"github.com/agentydragon/ducktape/agentplane/app/internal/server/testauth"
	upstreamproxy "github.com/agentydragon/ducktape/agentplane/app/internal/upstream"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/encoding/protojson"
)

func newStreamTestAuth(t *testing.T) *testauth.Fixture {
	t.Helper()
	return testauth.New(t)
}

func TestOperatorStreamsAndProxyRoutesRejectWorkloadTokens(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication.Auth)
	streamhttp.RegisterRoutes(server, server.DrainDone(), streamhttp.Dependencies{})
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
			request.Header.Set("Authorization", "Bearer workload-token")
			response := httptest.NewRecorder()
			server.Handler(authentication.Auth).ServeHTTP(response, request)
			if response.Code != http.StatusUnauthorized {
				t.Fatalf("workload-token access to operator route = %d %s, want 401", response.Code, response.Body.String())
			}
		})
	}
	for _, route := range []struct{ method, path string }{
		{http.MethodPost, "/upstream/policy/v1/sandboxes/tools/demo/policy-bindings"},
		{http.MethodDelete, "/upstream/policy/v1/sandboxes/tools/demo/policy-bindings/grant?sandboxUid=sandbox-uid&bindingUid=binding-uid"},
	} {
		t.Run(route.method+" "+route.path, func(t *testing.T) {
			request := httptest.NewRequest(route.method, route.path, nil)
			request.Header.Set("Authorization", "Bearer workload-token")
			response := httptest.NewRecorder()
			server.Handler(authentication.Auth).ServeHTTP(response, request)
			if response.Code != http.StatusUnauthorized {
				t.Fatalf("workload-token access to operator policy mutation = %d %s, want 401", response.Code, response.Body.String())
			}
		})
	}
}

func TestHistoryFacadeRequiresAnAuthenticatedCaller(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication.Auth)
	var gotPath, gotBearer string
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, _ *http.Request, _ context.Context, path, bearer string) error {
		gotPath, gotBearer = path, bearer
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, err := io.WriteString(w, `{"observations":[]}`)
		return err
	}}
	streamhttp.RegisterRoutes(server, server.DrainDone(), streamhttp.Dependencies{HistoryHTTP: upstream})

	path := "/upstream/history/v1/sessions/9b6d568a-2253-43d7-a7d4-190e98359594/observations"
	unauthenticated := httptest.NewRecorder()
	server.Handler(authentication.Auth).ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodGet, path, nil))
	if unauthenticated.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated History facade status = %d, want %d", unauthenticated.Code, http.StatusUnauthorized)
	}

	request := httptest.NewRequest(http.MethodGet, path+"?limit=30", nil)
	authentication.Authorize(request)
	response := httptest.NewRecorder()
	server.Handler(authentication.Auth).ServeHTTP(response, request)
	if response.Code != http.StatusOK || response.Body.String() != `{"observations":[]}` {
		t.Fatalf("authenticated History facade response = %d %q", response.Code, response.Body.String())
	}
	if gotPath != "v1/sessions/9b6d568a-2253-43d7-a7d4-190e98359594/observations" || gotBearer != "" {
		t.Fatalf("History upstream path/bearer = %q/%q", gotPath, gotBearer)
	}
}

type typedHistoryServer struct {
	historypb.UnimplementedHistoryServiceServer
	requests     chan *historypb.ReadThreadEntitiesRequest
	holdSelected bool
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
	app := newHTTPServer(authentication.Auth)
	streamhttp.RegisterRoutes(app, app.DrainDone(), streamhttp.Dependencies{HistoryHTTP: upstreamproxy.NewHandlerUpstream(generated, "")})
	const id = "9b6d568a-2253-43d7-a7d4-190e98359594"
	body, err := protojson.Marshal(&historypb.ReadThreadEntitiesRequest{Window: &historypb.ReadThreadEntitiesRequest_Range{
		Range: &historypb.EntityRange{StartEntityIndex: 2, Limit: 1},
	}})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/upstream/history/v1/sessions/"+id+"/entities", bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	authentication.Authorize(request)
	response := httptest.NewRecorder()
	app.Handler(authentication.Auth).ServeHTTP(response, request)
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
	authentication.Authorize(request)
	response = httptest.NewRecorder()
	app.Handler(authentication.Auth).ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("keyed entity facade status = %d: %s", response.Code, response.Body.String())
	}
	received = <-servicer.requests
	if len(received.GetKeys().GetKeys()) != 1 || received.GetKeys().GetKeys()[0].GetEntityKind() != historypb.ThreadEntityKind_THREAD_ENTITY_KIND_ITEM {
		t.Fatalf("keyed entity request = %v", received)
	}
}

func TestDispatcherFacadeRequiresOperatorSessionForCommands(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication.Auth)
	forwarded := 0
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, _ *http.Request, _ context.Context, _, _ string) error {
		forwarded++
		w.WriteHeader(http.StatusAccepted)
		return nil
	}}
	streamhttp.RegisterRoutes(server, server.DrainDone(), streamhttp.Dependencies{DispatcherHTTP: upstream})
	const prefix = "/upstream/dispatcher/v1/sessions/9b6d568a-2253-43d7-a7d4-190e98359594"
	const body = `{"commandId":"stable","sandbox":"demo","sandboxUid":"uid-1"}`

	unauthenticated := httptest.NewRecorder()
	server.Handler(authentication.Auth).ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodPost, prefix+":submitInput", strings.NewReader(body)))
	if unauthenticated.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous command status = %d, want 401", unauthenticated.Code)
	}
	for _, method := range []string{"submitInput", "changeModel", "changeReasoningEffort", "interruptTurn"} {
		request := httptest.NewRequest(http.MethodPost, prefix+":"+method, strings.NewReader(body))
		authentication.Authorize(request)
		response := httptest.NewRecorder()
		server.Handler(authentication.Auth).ServeHTTP(response, request)
		if response.Code != http.StatusForbidden {
			t.Fatalf("operator bearer without a browser session for %s = %d %s, want 403", method, response.Code, response.Body.String())
		}
	}
	openingRequest := httptest.NewRequest(http.MethodGet, prefix+"/opening", nil)
	authentication.Authorize(openingRequest)
	openingResponse := httptest.NewRecorder()
	server.Handler(authentication.Auth).ServeHTTP(openingResponse, openingRequest)
	if openingResponse.Code != http.StatusForbidden {
		t.Fatalf("operator bearer without a browser session for opening read = %d %s, want 403", openingResponse.Code, openingResponse.Body.String())
	}
	if forwarded != 0 {
		t.Fatalf("workload request reached Dispatcher %d times", forwarded)
	}
}

type streamTestUpstream struct {
	forward func(http.ResponseWriter, *http.Request, context.Context, string, string) error
}

func (f *streamTestUpstream) ForwardHTTP(w http.ResponseWriter, request *http.Request, ctx context.Context, path, bearer string) error {
	return f.forward(w, request, ctx, path, bearer)
}

func TestEnvironmentFacadeRequiresAppCallerAndForwardsManagementAndWatch(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication.Auth)
	var gotPath, gotMethod, gotQuery string
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, request *http.Request, _ context.Context, path, bearer string) error {
		gotPath, gotMethod, gotQuery = path, request.Method, request.URL.RawQuery
		if bearer != "" {
			t.Fatalf("unexpected forwarded bearer: %q", bearer)
		}
		w.WriteHeader(http.StatusOK)
		return nil
	}}
	streamhttp.RegisterRoutes(server, server.DrainDone(), streamhttp.Dependencies{EnvironmentHTTP: upstream})
	path := "/upstream/environments/v1/environments:watch"
	unauthenticated := httptest.NewRecorder()
	server.Handler(authentication.Auth).ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodGet, path, nil))
	if unauthenticated.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous watch = %d", unauthenticated.Code)
	}
	request := httptest.NewRequest(http.MethodGet, path, nil)
	authentication.Authorize(request)
	response := httptest.NewRecorder()
	server.Handler(authentication.Auth).ServeHTTP(response, request)
	if response.Code != http.StatusOK || gotPath != "v1/environments:watch" {
		t.Fatalf("watch = %d, path = %q", response.Code, gotPath)
	}
	for _, methodPath := range []struct{ method, path, wantPath, wantQuery string }{
		{http.MethodGet, "/upstream/environments/v1/environment-templates", "v1/environment-templates", ""},
		{http.MethodGet, "/upstream/environments/v1/environments/test-envs/workspace:identity-record?target.uid=selected-uid", "v1/environments/test-envs/workspace:identity-record", "target.uid=selected-uid"},
		{http.MethodGet, "/upstream/environments/v1/environments/test-envs/workspace:runtime-diagnostics?target.uid=selected-uid", "v1/environments/test-envs/workspace:runtime-diagnostics", "target.uid=selected-uid"},
		{http.MethodPost, "/upstream/environments/v1/environments", "v1/environments", ""},
		{http.MethodDelete, "/upstream/environments/v1/environments/test-envs/workspace?target.uid=selected-uid", "v1/environments/test-envs/workspace", "target.uid=selected-uid"},
		{http.MethodPatch, "/upstream/environments/v1/environments/test-envs/workspace:idle-suspend", "v1/environments/test-envs/workspace:idle-suspend", ""},
		{http.MethodPatch, "/upstream/environments/v1/environments/test-envs/workspace:identity", "v1/environments/test-envs/workspace:identity", ""},
	} {
		request := httptest.NewRequest(methodPath.method, methodPath.path, nil)
		authentication.Authorize(request)
		response := httptest.NewRecorder()
		server.Handler(authentication.Auth).ServeHTTP(response, request)
		if response.Code != http.StatusOK || gotPath != methodPath.wantPath || gotMethod != methodPath.method || gotQuery != methodPath.wantQuery {
			t.Fatalf("%s %s = %d, forwarded %s %s?%s", methodPath.method, methodPath.path, response.Code, gotMethod, gotPath, gotQuery)
		}
	}
}

func TestEnvironmentGetRequiresAppCallerAndForwardsOnlyTarget(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication.Auth)
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
	streamhttp.RegisterRoutes(server, server.DrainDone(), streamhttp.Dependencies{EnvironmentHTTP: upstream})
	path := "/upstream/environments/v1/environments/team-a/sandbox-1?target.uid=22222222-2222-4222-8222-222222222222"
	unauthenticated := httptest.NewRecorder()
	server.Handler(authentication.Auth).ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodGet, path, nil))
	if unauthenticated.Code != http.StatusUnauthorized || calls != 0 {
		t.Fatalf("anonymous get = %d, upstream calls = %d", unauthenticated.Code, calls)
	}

	request := httptest.NewRequest(http.MethodGet, path, nil)
	authentication.Authorize(request)
	response := httptest.NewRecorder()
	server.Handler(authentication.Auth).ServeHTTP(response, request)
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
	server := newHTTPServer(authentication.Auth)
	generated, err := facade.NewHandler(t.Context(), "127.0.0.1:1", time.Second, environmentpb.RegisterEnvironmentServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	streamhttp.RegisterRoutes(server, server.DrainDone(), streamhttp.Dependencies{EnvironmentHTTP: upstreamproxy.NewHandlerUpstream(generated, "")})
	handler := server.Handler(authentication.Auth)
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
		authentication.Authorize(request)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != test.status {
			t.Errorf("%s %s = %d, want %d", test.method, test.path, response.Code, test.status)
		}
	}
}

func TestEnvironmentGetCancelsUpstreamWithRequest(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := newHTTPServer(authentication.Auth)
	started := make(chan context.Context, 1)
	canceled := make(chan error, 1)
	upstream := &streamTestUpstream{forward: func(_ http.ResponseWriter, _ *http.Request, ctx context.Context, _, _ string) error {
		started <- ctx
		<-ctx.Done()
		canceled <- ctx.Err()
		return ctx.Err()
	}}
	streamhttp.RegisterRoutes(server, server.DrainDone(), streamhttp.Dependencies{EnvironmentHTTP: upstream})
	ctx, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodGet, "/upstream/environments/v1/environments/team-a/sandbox-1?target.uid=uid", nil).WithContext(ctx)
	authentication.Authorize(request)
	response := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		server.Handler(authentication.Auth).ServeHTTP(response, request)
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

func (s *typedHistoryServer) WatchSelectedSessionSummaries(req *historypb.WatchSelectedSessionSummariesRequest, stream grpc.ServerStreamingServer[historypb.WatchSelectedSessionSummariesResponse]) error {
	if err := stream.Send(&historypb.WatchSelectedSessionSummariesResponse{Observation: &historypb.WatchSelectedSessionSummariesResponse_Snapshot{Snapshot: &historypb.BatchGetSessionSummariesResponse{Summaries: []*historypb.SessionSummary{{SessionId: req.GetSessionIds()[0]}}}}}); err != nil {
		return err
	}
	if s.holdSelected {
		<-stream.Context().Done()
	}
	return nil
}

func TestHistorySelectedSummariesGeneratedFacade(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	grpcServer := grpc.NewServer()
	historypb.RegisterHistoryServiceServer(grpcServer, &typedHistoryServer{holdSelected: true})
	go func() { _ = grpcServer.Serve(listener) }()
	t.Cleanup(grpcServer.Stop)
	generated, err := facade.NewHandler(t.Context(), listener.Addr().String(), time.Second, historypb.RegisterHistoryServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	authentication := newStreamTestAuth(t)
	app := newHTTPServer(authentication.Auth)
	streamhttp.RegisterRoutes(app, app.DrainDone(), streamhttp.Dependencies{HistoryHTTP: upstreamproxy.NewHandlerUpstream(generated, "")})
	httpServer := httptest.NewServer(app.Handler(authentication.Auth))
	defer httpServer.Close()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, httpServer.URL+"/upstream/history/v1/session-summaries:watchSelected?session_ids=11111111-1111-1111-1111-111111111111", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Accept", "application/x-ndjson")
	authentication.Authorize(request)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	data := make([]byte, 1024)
	n, err := response.Body.Read(data)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || !strings.Contains(string(data[:n]), "11111111-1111-1111-1111-111111111111") {
		t.Fatalf("selected summary facade: %d %s", response.StatusCode, data[:n])
	}
}

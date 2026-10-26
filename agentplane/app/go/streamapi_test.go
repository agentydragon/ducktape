package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/historydata"
	"github.com/agentydragon/ducktape/agentplane/app/go/liveinventory"
	"github.com/agentydragon/ducktape/agentplane/app/go/proxy"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	"github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestWriteSSEPreservesEventIDAndJSONData(t *testing.T) {
	response := httptest.NewRecorder()
	if err := startSSE(response); err != nil {
		t.Fatal(err)
	}
	cursor := uint64(42)
	if err := writeSSE(response, "event", map[string]any{"kind": "tool", "text": "hello"}, &cursor); err != nil {
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
	for _, want := range []string{"event: event\n", "id: 42\n", `data: {"kind":"tool","text":"hello"}`, "\n\n"} {
		if !strings.Contains(response.Body.String(), want) {
			t.Fatalf("SSE frame %q does not contain %q", response.Body.String(), want)
		}
	}
}

func TestLiveWatchHealthOmitsCacheSyncImplementationDetail(t *testing.T) {
	got := healthView(liveinventory.WatchHealth{
		Fresh: true, Synced: true, StaleAfterSeconds: 30,
		RefreshedSecondsAgo: map[string]float64{"pods": 0.1},
	})
	if !got.Fresh || got.StaleAfterSeconds != 30 || got.RefreshedSecondsAgo["pods"] != 0.1 {
		t.Fatalf("health projection = %#v", got)
	}
	encoded, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "synced") {
		t.Fatalf("Python-compatible health projection leaked Synced: %s", encoded)
	}
}

func TestDecisionViewsPreserveOptionalNullsAndOutcome(t *testing.T) {
	reason, binding, policy, address := "no_rule", "binding-a", "policy-a", "192.0.2.1"
	rule := int32(3)
	got := decisionViews([]*egress_adminpb.Decision{
		{
			At:     timestamppb.New(time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)),
			Method: "GET", Host: "example.test", Port: 443,
			Outcome: egress_adminpb.Decision_OUTCOME_DENY,
			Reason:  &reason, Binding: &binding, Policy: &policy, Rule: &rule,
			Substituted: true, Address: &address,
		},
		{Method: "POST", Host: "other.test", Port: 80, Outcome: egress_adminpb.Decision_OUTCOME_ALLOW},
	})
	if len(got) != 2 || got[0].Outcome != "deny" || got[1].Outcome != "allow" {
		t.Fatalf("decision outcomes = %#v", got)
	}
	if got[0].Reason == nil || *got[0].Reason != reason || got[0].Rule == nil || *got[0].Rule != rule {
		t.Fatalf("decision optional fields = %#v", got[0])
	}
	if got[1].Reason != nil || got[1].Binding != nil || got[1].Policy != nil || got[1].Rule != nil || got[1].Address != nil {
		t.Fatalf("absent decision fields were populated: %#v", got[1])
	}
}

type streamTestLive struct {
	mu        sync.Mutex
	snapshots []liveinventory.Snapshot
	reads     int
	changes   chan struct{}
}

func (f *streamTestLive) Snapshot(context.Context) (liveinventory.Snapshot, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	index := f.reads
	f.reads++
	if index >= len(f.snapshots) {
		index = len(f.snapshots) - 1
	}
	return f.snapshots[index], nil
}

func (f *streamTestLive) Subscribe() (<-chan struct{}, func()) {
	return f.changes, func() {}
}

func TestLiveSandboxStreamSendsInitialUpdateAndStaleHealth(t *testing.T) {
	live := &streamTestLive{
		snapshots: []liveinventory.Snapshot{
			{Sandboxes: []sandboxapi.Sandbox{{ObjectMeta: metav1.ObjectMeta{Namespace: "tools", Name: "demo"}}}, Watch: liveinventory.WatchHealth{Fresh: true, StaleAfterSeconds: 30}},
			{Sandboxes: []sandboxapi.Sandbox{{ObjectMeta: metav1.ObjectMeta{Namespace: "tools", Name: "demo"}}}, Watch: liveinventory.WatchHealth{Fresh: false, StaleAfterSeconds: 30}},
		},
		changes: make(chan struct{}, 1),
	}
	server := NewHTTPServer(nil)
	routes := &streamingRoutes{server: server, deps: StreamingDependencies{Live: live}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	request := httptest.NewRequest(http.MethodGet, "/live/sandboxes", nil).WithContext(ctx)
	response := newStreamingRecorder()
	done := make(chan struct{})
	go func() {
		routes.liveSandboxes(response, request)
		close(done)
	}()
	waitForStreamText(t, response, `"fresh":true`)
	live.changes <- struct{}{}
	waitForStreamText(t, response, `"fresh":false`)
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("live sandbox stream did not stop after request cancellation")
	}
	body := response.String()
	if !strings.Contains(body, `"name":"demo"`) || strings.Count(body, "event: snapshot") != 2 {
		t.Fatalf("live sandbox frames = %s", body)
	}
}

type decisionTestStream struct {
	grpc.ServerStreamingClient[egress_adminpb.WatchDecisionsResponse]
	messages []*egress_adminpb.WatchDecisionsResponse
	err      error
	index    int
}

func (f *decisionTestStream) Recv() (*egress_adminpb.WatchDecisionsResponse, error) {
	if f.index < len(f.messages) {
		message := f.messages[f.index]
		f.index++
		return message, nil
	}
	return nil, f.err
}

type decisionTestWatcher struct {
	stream  *decisionTestStream
	request *egress_adminpb.WatchDecisionsRequest
}

func (f *decisionTestWatcher) WatchDecisions(_ context.Context, request *egress_adminpb.WatchDecisionsRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[egress_adminpb.WatchDecisionsResponse], error) {
	f.request = request
	return f.stream, nil
}

func TestEgressDecisionStreamSnapshotsUpdatesAndDisconnect(t *testing.T) {
	live := &streamTestLive{
		snapshots: []liveinventory.Snapshot{{Sandboxes: []sandboxapi.Sandbox{{ObjectMeta: metav1.ObjectMeta{Namespace: "tools", Name: "demo", UID: "sandbox-uid"}}}}},
		changes:   make(chan struct{}),
	}
	watcher := &decisionTestWatcher{stream: &decisionTestStream{
		messages: []*egress_adminpb.WatchDecisionsResponse{
			{Decisions: []*egress_adminpb.Decision{{Method: "GET", Outcome: egress_adminpb.Decision_OUTCOME_ALLOW}}},
			{Decisions: []*egress_adminpb.Decision{{Method: "POST", Outcome: egress_adminpb.Decision_OUTCOME_DENY}}},
		},
		err: status.Error(codes.Unavailable, "watch broke"),
	}}
	server := NewHTTPServer(nil)
	routes := &streamingRoutes{server: server, deps: StreamingDependencies{Live: live, Egress: watcher}}
	ctx, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodGet, "/live/sandboxes/demo/egress/decisions", nil).WithContext(ctx)
	request.SetPathValue("name", "demo")
	response := newStreamingRecorder()
	done := make(chan struct{})
	go func() {
		routes.liveDecisions(response, request)
		close(done)
	}()
	waitForStreamText(t, response, "Unavailable: watch broke")
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("egress decision stream did not stop after request cancellation")
	}
	if watcher.request == nil || watcher.request.GetFilter().GetSubject().GetNamespace() != "tools" || watcher.request.GetFilter().GetSubject().GetName() != "demo" {
		t.Fatalf("decision watch was not scoped to the Sandbox ServiceAccount: %+v", watcher.request)
	}
	body := response.String()
	for _, want := range []string{`"outcome":"allow"`, `"outcome":"deny"`, `"watching":false`} {
		if !strings.Contains(body, want) {
			t.Fatalf("decision stream frame %q missing from %s", want, body)
		}
	}
}

func TestNotificationRelaySendsSnapshotAndStopsForReconnect(t *testing.T) {
	response := newStreamingRecorder()
	routes := &streamingRoutes{}
	messages := []proto.Message{&emptypb.Empty{}, &emptypb.Empty{}}
	index := 0
	done := make(chan struct{})
	go func() {
		routes.relayNotificationStream(response, context.Background(), "Notification", func() (proto.Message, error) {
			message := messages[index]
			index++
			return message, nil
		}, func(proto.Message) (proto.Message, bool) {
			if index == 1 {
				return &emptypb.Empty{}, false
			}
			return nil, true
		}, time.Second)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("notification relay did not stop after planned reconnect")
	}
	body := response.String()
	if strings.Count(body, "event: snapshot") != 1 || strings.Contains(body, "reconnect") {
		t.Fatalf("notification reconnect stream = %s", body)
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
	server := NewHTTPServer(authentication)
	RegisterStreamingRoutes(server, StreamingDependencies{})
	ids := uuid.NewString()
	for _, path := range []string{
		"/threads/notices/stream?thread_id=" + ids,
		"/sandboxes/demo/notifications/stream",
		"/sandboxes/demo/console/stream",
		"/upstream/dispatcher/v1/items",
		"/upstream/actions/v1/items",
		"/upstream/connections/v1/items",
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

type streamTestThreadStore struct {
	threadReadStore
	row *threads.Thread
}

func (f *streamTestThreadStore) GetThread(context.Context, uuid.UUID) (*threads.Thread, error) {
	return f.row, nil
}

func (f *streamTestThreadStore) ArchivedCount(context.Context) (int, error) { return 0, nil }

type streamTestHistory struct {
	threadReadHistory
	mu          sync.Mutex
	firstRead   chan struct{}
	thirdReady  bool
	feedSummary historydata.FoldSummary
}

func (f *streamTestHistory) GetSummary(context.Context, uuid.UUID) (historydata.FoldSummary, error) {
	return f.feedSummary, nil
}

func (f *streamTestHistory) ReadWatermark(context.Context, uuid.UUID) (uint64, error) {
	return 100, nil
}

func (f *streamTestHistory) Events(_ context.Context, _ uuid.UUID, after uint64, _ int) ([]*eventlogpb.EventEntry, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if after == 0 {
		select {
		case <-f.firstRead:
		default:
			close(f.firstRead)
		}
		return []*eventlogpb.EventEntry{{Cursor: 1}, {Cursor: 2}}, nil
	}
	if after == 2 && f.thirdReady {
		return []*eventlogpb.EventEntry{{Cursor: 3}}, nil
	}
	return nil, nil
}

type streamTestChanges struct{ changes chan struct{} }

func (f *streamTestChanges) Subscribe(...string) (<-chan struct{}, func(), error) {
	return f.changes, func() {}, nil
}

func TestThreadEventStreamReplaysHeartbeatsDrainsAndRequiresCaller(t *testing.T) {
	authentication := newStreamTestAuth(t)
	threadID := uuid.New()
	history := &streamTestHistory{
		firstRead: make(chan struct{}),
		feedSummary: historydata.FoldSummary{Feed: &historydata.FeedSnapshot{
			Status:   historypb.FeedStatus_FEED_STATUS_ACTIVE,
			Attached: &runnerpb.Attached{SessionId: threadID.String(), LastCursor: 3},
		}},
	}
	updates := &streamTestChanges{changes: make(chan struct{}, 1)}
	server := NewHTTPServer(authentication)
	RegisterStreamingRoutes(server, StreamingDependencies{
		Threads: &streamTestThreadStore{row: &threads.Thread{ID: threadID}},
		History: history, Updates: updates, ThreadHeartbeat: 20 * time.Millisecond,
	})
	url := "/threads/" + threadID.String() + "/events/stream?after=0"
	unauthenticated := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodGet, url, nil))
	if unauthenticated.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous thread stream status = %d, want 401", unauthenticated.Code)
	}

	response := newStreamingRecorder()
	request := httptest.NewRequest(http.MethodGet, url, nil)
	request.Header.Set("Authorization", "Bearer accepted")
	done := make(chan struct{})
	go func() {
		server.Handler(authentication).ServeHTTP(response, request)
		close(done)
	}()
	for _, want := range []string{"event: attached", "id: 1", "id: 2"} {
		waitForStreamText(t, response, want)
	}
	<-history.firstRead
	history.mu.Lock()
	history.thirdReady = true
	history.mu.Unlock()
	updates.changes <- struct{}{}
	waitForStreamText(t, response, "id: 3")
	waitForStreamText(t, response, ": keepalive")
	server.BeginDrain()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("thread event stream did not end during server drain")
	}
	if body := response.String(); !strings.Contains(body, "id: 1") || !strings.Contains(body, "id: 2") || !strings.Contains(body, "id: 3") || !strings.Contains(body, ": keepalive") {
		t.Fatalf("thread event stream did not replay and drain cleanly: %s", body)
	}
}

type streamTestUpstream struct {
	forward func(http.ResponseWriter, *http.Request, context.Context, string, string, bool, func(error)) error
}

func (f *streamTestUpstream) ForwardHTTP(w http.ResponseWriter, request *http.Request, ctx context.Context, path, bearer string, rejectRedirects bool, onError func(error)) error {
	return f.forward(w, request, ctx, path, bearer, rejectRedirects, onError)
}

func TestUpstreamRouteForwardersPreserveFacadeAndDispatcherResponses(t *testing.T) {
	routes := &streamingRoutes{}
	var bodyText string
	var gotMethod, gotPath, gotQuery, gotBearer string
	var gotRejectRedirects bool
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, request *http.Request, _ context.Context, path, bearer string, rejectRedirects bool, _ func(error)) error {
		gotMethod, gotPath, gotQuery, gotBearer, gotRejectRedirects = request.Method, path, request.URL.RawQuery, bearer, rejectRedirects
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
	routes.forwardFacade(response, request, context.Background(), upstream, "Action facade", "v1/items", "federated-token")
	if response.Code != http.StatusAccepted || response.Body.String() != `{"ok":true}` {
		t.Fatalf("action facade response = %d %q", response.Code, response.Body.String())
	}
	if gotMethod != http.MethodPost || gotPath != "v1/items" || gotQuery != "x=1" || gotBearer != "federated-token" || !gotRejectRedirects || bodyText != "streamed request" {
		t.Fatalf("facade request = %s %s?%s bearer=%q body=%q", gotMethod, gotPath, gotQuery, gotBearer, bodyText)
	}
	if response.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("facade response headers = %#v", response.Header())
	}

	redirect := &streamTestUpstream{forward: func(_ http.ResponseWriter, _ *http.Request, _ context.Context, _, _ string, reject bool, onError func(error)) error {
		if !reject {
			t.Error("facade proxy did not reject redirects")
		}
		onError(proxy.ErrUpstreamRedirect)
		return nil
	}}
	redirectResponse := httptest.NewRecorder()
	routes.forwardFacade(redirectResponse, request, context.Background(), redirect, "Connection facade", "v1/items", "token")
	if redirectResponse.Code != http.StatusBadGateway || !strings.Contains(redirectResponse.Body.String(), "redirects are not accepted") {
		t.Fatalf("facade redirect = %d %s", redirectResponse.Code, redirectResponse.Body.String())
	}

	var dispatcherPath, dispatcherQuery string
	dispatcher := &streamTestUpstream{forward: func(w http.ResponseWriter, request *http.Request, _ context.Context, path, bearer string, reject bool, _ func(error)) error {
		dispatcherPath, dispatcherQuery = path, request.URL.RawQuery
		if bearer != "" || reject {
			t.Errorf("dispatcher forward options = bearer %q, reject redirects %v", bearer, reject)
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
	upstream := &streamTestUpstream{forward: func(_ http.ResponseWriter, _ *http.Request, ctx context.Context, _, _ string, _ bool, _ func(error)) error {
		close(entered)
		<-ctx.Done()
		return ctx.Err()
	}}
	ctx, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodGet, "/upstream/actions/v1/items", nil)
	response := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		(&streamingRoutes{}).forwardFacade(response, request, ctx, upstream, "Action facade", "v1/items", "token")
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

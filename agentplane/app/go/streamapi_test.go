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
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	"github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	environmentpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/environmentpb"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
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
	since := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	got := healthView(watchhealth.Status{
		Synced: true, Failing: map[watchhealth.Kind]watchhealth.Failure{"pods": {Error: "test-failure", Since: since}},
	})
	if got.Healthy || got.Failing["pods"] != (kindFailureView{Error: "test-failure", Since: since}) {
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

type streamTestEnvironments struct {
	mu         sync.Mutex
	descriptor *environmentpb.EnvironmentDescriptor
	err        error
	changes    chan struct{}
}

func (f *streamTestEnvironments) GetEnvironment(_ context.Context, request *environmentpb.GetEnvironmentRequest, _ ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error) {
	target := request.GetTarget()
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return nil, f.err
	}
	if f.descriptor == nil || !proto.Equal(f.descriptor.GetTarget(), target) {
		return nil, status.Error(codes.NotFound, "Environment target UID is no longer current")
	}
	return proto.Clone(f.descriptor).(*environmentpb.EnvironmentDescriptor), nil
}

func (f *streamTestEnvironments) Watch(ctx context.Context, target *environmentpb.TargetHandle) <-chan sandboxes.EnvironmentObservation {
	observations := make(chan sandboxes.EnvironmentObservation)
	go func() {
		defer close(observations)
		for {
			f.mu.Lock()
			var value *environmentpb.EnvironmentDescriptor
			var err error
			if f.err != nil {
				err = f.err
			} else if f.descriptor == nil || !proto.Equal(f.descriptor.GetTarget(), target) {
				err = status.Error(codes.NotFound, "Environment not found")
			} else {
				value = proto.Clone(f.descriptor).(*environmentpb.EnvironmentDescriptor)
			}
			f.mu.Unlock()
			select {
			case observations <- sandboxes.EnvironmentObservation{Descriptor: value, Err: err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
			select {
			case <-f.changes:
			case <-ctx.Done():
				return
			}
		}
	}()
	return observations
}

func (f *streamTestEnvironments) set(descriptor *environmentpb.EnvironmentDescriptor) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if descriptor == nil {
		f.descriptor = nil
		return
	}
	f.descriptor = proto.Clone(descriptor).(*environmentpb.EnvironmentDescriptor)
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

func (f *streamTestLive) Health() watchhealth.Status {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.snapshots[len(f.snapshots)-1].Watch
}

func (f *streamTestLive) Subscribe() (<-chan struct{}, func()) {
	return f.changes, func() {}
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
		snapshots: []liveinventory.Snapshot{{}},
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
	environments := &streamTestEnvironments{descriptor: testEnvironment("tools", "demo", "sandbox-uid", "tools", "demo")}
	routes := &streamingRoutes{server: server, deps: StreamingDependencies{SandboxNamespace: "tools", Live: live, Sandboxes: environments, Egress: watcher}}
	ctx, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodGet, "/live/sandboxes/demo/egress/decisions?sandbox_uid=sandbox-uid", nil).WithContext(ctx)
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
		"/upstream/notifications/v1/operator/notices:watch?sessionIds=" + ids,
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

func TestHistoryFacadeRequiresAnAuthenticatedCaller(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := NewHTTPServer(authentication)
	var gotPath, gotBearer string
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, _ *http.Request, _ context.Context, path, bearer string) error {
		gotPath, gotBearer = path, bearer
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, err := io.WriteString(w, `{"observations":[]}`)
		return err
	}}
	RegisterStreamingRoutes(server, StreamingDependencies{HistoryHTTP: upstream})

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

func TestDispatcherFacadeRejectsWorkloadCommandPOSTs(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := NewHTTPServer(authentication)
	forwarded := 0
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, _ *http.Request, _ context.Context, _, _ string) error {
		forwarded++
		w.WriteHeader(http.StatusAccepted)
		return nil
	}}
	RegisterStreamingRoutes(server, StreamingDependencies{DispatcherHTTP: upstream})
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

type streamTestThreadStore struct {
	threadReadStore
	row  *threads.Thread
	rows []threads.Thread
}

func (f *streamTestThreadStore) GetThread(context.Context, uuid.UUID) (*threads.Thread, error) {
	return f.row, nil
}

func (f *streamTestThreadStore) ListPage(context.Context, int, *threads.PageCursor, bool) (threads.Page, error) {
	return threads.Page{Threads: f.rows}, nil
}

func (f *streamTestThreadStore) ArchivedCount(context.Context) (int, error) { return 0, nil }

type streamTestHistory struct {
	threadReadHistory
	feedSummary historydata.FoldSummary
	summaries   map[uuid.UUID]historydata.FoldSummary
	sandboxes   map[uuid.UUID]string
	sandboxUIDs map[uuid.UUID]string
	summaryRead chan struct{}
}

func (f *streamTestHistory) GetSummary(context.Context, uuid.UUID) (historydata.FoldSummary, error) {
	if f.summaryRead != nil {
		f.summaryRead <- struct{}{}
	}
	return f.feedSummary, nil
}

func (f *streamTestHistory) BatchGetSummaries(_ context.Context, ids []uuid.UUID) (map[uuid.UUID]historydata.FoldSummary, error) {
	result := make(map[uuid.UUID]historydata.FoldSummary, len(ids))
	for _, id := range ids {
		result[id] = f.summaries[id]
	}
	return result, nil
}

func (f *streamTestHistory) FindSessions(_ context.Context, sandboxName, sandboxNameContains, sandboxUID *string) (map[uuid.UUID]string, error) {
	result := make(map[uuid.UUID]string)
	for id, sandbox := range f.sandboxes {
		if sandboxName != nil && sandbox == *sandboxName {
			result[id] = sandbox
		}
		if sandboxNameContains != nil && strings.Contains(strings.ToLower(sandbox), strings.ToLower(*sandboxNameContains)) {
			result[id] = sandbox
		}
		if sandboxUID != nil && f.sandboxUIDs[id] == *sandboxUID {
			result[id] = sandbox
		}
	}
	return result, nil
}

func (f *streamTestHistory) ReadWatermark(context.Context, uuid.UUID) (uint64, error) {
	return 100, nil
}

type streamTestChanges struct {
	changes chan struct{}
	// unsubscribed, when set, receives each ended subscription.
	unsubscribed chan struct{}
}

func (f *streamTestChanges) Subscribe(...string) (<-chan struct{}, func(), error) {
	return f.changes, func() {
		if f.unsubscribed != nil {
			f.unsubscribed <- struct{}{}
		}
	}, nil
}

type threadFollowResult struct {
	response *historypb.FollowSessionResponse
	err      error
}

type threadFollowStream struct {
	grpc.ServerStreamingClient[historypb.FollowSessionResponse]
	ctx       context.Context
	responses chan threadFollowResult
}

func (f *threadFollowStream) Recv() (*historypb.FollowSessionResponse, error) {
	select {
	case <-f.ctx.Done():
		return nil, status.FromContextError(f.ctx.Err()).Err()
	case result := <-f.responses:
		return result.response, result.err
	}
}

func (f *threadFollowStream) send(response *historypb.FollowSessionResponse) {
	f.responses <- threadFollowResult{response: response}
}

type threadFollowScript struct {
	responses []*historypb.FollowSessionResponse
	openError error
}

type threadFollowClient struct {
	mu       sync.Mutex
	scripts  []threadFollowScript
	requests []*historypb.FollowSessionRequest
	calls    chan *historypb.FollowSessionRequest
	streams  chan *threadFollowStream
}

func newThreadFollowClient(scripts ...threadFollowScript) *threadFollowClient {
	return &threadFollowClient{
		scripts: scripts, calls: make(chan *historypb.FollowSessionRequest, 16), streams: make(chan *threadFollowStream, 16),
	}
}

func (f *threadFollowClient) FollowSession(ctx context.Context, request *historypb.FollowSessionRequest) (grpc.ServerStreamingClient[historypb.FollowSessionResponse], error) {
	copy := proto.Clone(request).(*historypb.FollowSessionRequest)
	f.mu.Lock()
	f.requests = append(f.requests, copy)
	if len(f.scripts) == 0 {
		f.mu.Unlock()
		f.calls <- copy
		return nil, status.Error(codes.Internal, "unexpected FollowSession call")
	}
	script := f.scripts[0]
	f.scripts = f.scripts[1:]
	f.mu.Unlock()
	f.calls <- copy
	if script.openError != nil {
		return nil, script.openError
	}
	stream := &threadFollowStream{ctx: ctx, responses: make(chan threadFollowResult, len(script.responses)+4)}
	for _, response := range script.responses {
		stream.send(response)
	}
	f.streams <- stream
	return stream, nil
}

func (f *threadFollowClient) requestSnapshot() []*historypb.FollowSessionRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	requests := make([]*historypb.FollowSessionRequest, len(f.requests))
	for index, request := range f.requests {
		requests[index] = proto.Clone(request).(*historypb.FollowSessionRequest)
	}
	return requests
}

func awaitFollowCall(t *testing.T, client *threadFollowClient) *historypb.FollowSessionRequest {
	t.Helper()
	select {
	case request := <-client.calls:
		return request
	case <-time.After(time.Second):
		t.Fatal("History FollowSession was not called")
		return nil
	}
}

func awaitFollowStream(t *testing.T, client *threadFollowClient) *threadFollowStream {
	t.Helper()
	select {
	case stream := <-client.streams:
		return stream
	case <-time.After(time.Second):
		t.Fatal("History FollowSession stream did not open")
		return nil
	}
}

func awaitSummaryRead(t *testing.T, history *streamTestHistory) {
	t.Helper()
	select {
	case <-history.summaryRead:
	case <-time.After(time.Second):
		t.Fatal("Thread event stream did not re-read its feed summary")
	}
}

func TestThreadEventStreamResumesFollowAtCoveredCursorAndDrains(t *testing.T) {
	authentication := newStreamTestAuth(t)
	threadID := uuid.New()
	const largeCursor = uint64(9007199254740993)
	history := &streamTestHistory{
		feedSummary: historydata.FoldSummary{Feed: &historydata.FeedSnapshot{
			Status:   historypb.FeedStatus_FEED_STATUS_ACTIVE,
			Attached: &sessionpb.Attached{SessionId: threadID.String(), LastCursor: largeCursor + 1},
		}, CommittedCursor: largeCursor + 1},
	}
	updates := &streamTestChanges{changes: make(chan struct{}, 1)}
	follower := newThreadFollowClient(
		threadFollowScript{openError: status.Error(codes.NotFound, "Session history is not recorded yet")},
		threadFollowScript{responses: []*historypb.FollowSessionResponse{
			{ThroughCursor: 2, Frame: &historypb.FollowSessionResponse_Settled{Settled: &historypb.SettledDeltas{CompletionCursor: 2}}},
			{ThroughCursor: 4, Frame: &historypb.FollowSessionResponse_Entries{Entries: &historypb.Entries{Entries: []*eventlogpb.EventEntry{{Cursor: 3}}}}},
			{ThroughCursor: 4, Frame: &historypb.FollowSessionResponse_Removed{Removed: &historypb.CursorRange{First: 4, Last: 5}}},
			{ThroughCursor: 6, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}},
			{ThroughCursor: 6, Frame: &historypb.FollowSessionResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}},
		}},
		threadFollowScript{responses: []*historypb.FollowSessionResponse{
			{ThroughCursor: largeCursor, Frame: &historypb.FollowSessionResponse_Entries{Entries: &historypb.Entries{Entries: []*eventlogpb.EventEntry{{Cursor: largeCursor}}}}},
			{ThroughCursor: largeCursor + 1, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}},
		}},
	)
	server := NewHTTPServer(authentication)
	RegisterStreamingRoutes(server, StreamingDependencies{
		Threads: &streamTestThreadStore{row: &threads.Thread{ID: threadID}},
		History: history, HistoryFollower: follower, Updates: updates, ThreadHeartbeat: 20 * time.Millisecond,
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
	request.Header.Set("Last-Event-ID", "2")
	request.URL.RawQuery = "after=1"
	done := make(chan struct{})
	go func() {
		server.Handler(authentication).ServeHTTP(response, request)
		close(done)
	}()
	firstCall := awaitFollowCall(t, follower)
	if firstCall.GetAfterCursor() != 2 {
		t.Fatalf("initial FollowSession cursor = %d, want Last-Event-ID 2", firstCall.GetAfterCursor())
	}
	updates.changes <- struct{}{}
	secondCall := awaitFollowCall(t, follower)
	if secondCall.GetAfterCursor() != 2 {
		t.Fatalf("pre-history FollowSession cursor = %d, want 2", secondCall.GetAfterCursor())
	}
	firstStream := awaitFollowStream(t, follower)
	waitForStreamText(t, response, "event: attached")
	waitForStreamText(t, response, "id: 3")
	thirdCall := awaitFollowCall(t, follower)
	if thirdCall.GetAfterCursor() != 6 {
		t.Fatalf("renewed FollowSession cursor = %d, want caught-up cursor 6", thirdCall.GetAfterCursor())
	}
	if firstStream.ctx.Err() == nil {
		t.Fatal("History FollowSession stream was not canceled after reconnect_required")
	}
	renewedStream := awaitFollowStream(t, follower)
	waitForStreamText(t, response, "id: 9007199254740993")
	waitForStreamTextAfter(t, response, "id: 9007199254740993", ": keepalive")
	var eventCursor string
	for _, frame := range strings.Split(response.String(), "\n\n") {
		var hasLargeID bool
		var data string
		for _, line := range strings.Split(frame, "\n") {
			if line == "id: 9007199254740993" {
				hasLargeID = true
			}
			if strings.HasPrefix(line, "data: ") {
				data = strings.TrimPrefix(line, "data: ")
			}
		}
		if !hasLargeID {
			continue
		}
		var payload map[string]json.RawMessage
		if err := json.Unmarshal([]byte(data), &payload); err != nil {
			t.Fatalf("large-cursor event payload is invalid JSON: %v", err)
		}
		if err := json.Unmarshal(payload["cursor"], &eventCursor); err != nil {
			t.Fatalf("large-cursor event cursor is not a JSON string: %v", err)
		}
		break
	}
	if eventCursor != "9007199254740993" {
		t.Fatalf("large-cursor event payload cursor = %q, want exact ProtoJSON string", eventCursor)
	}
	server.BeginDrain()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("thread event stream did not end during server drain")
	}
	if renewedStream.ctx.Err() == nil {
		t.Fatal("renewed History FollowSession stream was not canceled during server drain")
	}
	if body := response.String(); strings.Contains(body, "event: end") || strings.Contains(body, "event: error") || strings.Contains(body, "event: settled") || strings.Contains(body, "event: removed") {
		t.Fatalf("Thread event stream changed its event contract: %s", body)
	}
	requests := follower.requestSnapshot()
	if len(requests) != 3 || !requests[0].GetFollow() || !requests[1].GetFollow() || !requests[2].GetFollow() {
		t.Fatalf("FollowSession requests = %+v, want three following calls", requests)
	}
	if body := response.String(); !strings.Contains(body, "id: 3") || !strings.Contains(body, "id: 9007199254740993") || !strings.Contains(body, ": keepalive") {
		t.Fatalf("thread event stream did not replay, renew, heartbeat and drain cleanly: %s", body)
	}
}

func TestThreadEventStreamWaitsForCoveredTerminalFeedAndAwaitedAdmission(t *testing.T) {
	authentication := newStreamTestAuth(t)
	threadID := uuid.New()
	history := &streamTestHistory{
		feedSummary: historydata.FoldSummary{
			CommittedCursor: 6,
			Feed: &historydata.FeedSnapshot{
				Status:   historypb.FeedStatus_FEED_STATUS_ENDED,
				Attached: &sessionpb.Attached{SessionId: threadID.String(), LastCursor: 6},
				End:      &historydata.FeedEnd{},
			},
		},
		summaryRead: make(chan struct{}, 8),
	}
	updates := &streamTestChanges{changes: make(chan struct{}, 1)}
	follower := newThreadFollowClient(threadFollowScript{})
	server := NewHTTPServer(authentication)
	RegisterStreamingRoutes(server, StreamingDependencies{
		Threads: &streamTestThreadStore{row: &threads.Thread{ID: threadID}},
		History: history, HistoryFollower: follower, Updates: updates,
	})
	request := httptest.NewRequest(http.MethodGet, "/threads/"+threadID.String()+"/events/stream?awaiting_command_id=wait-for-this", nil)
	request.Header.Set("Authorization", "Bearer accepted")
	response := newStreamingRecorder()
	done := make(chan struct{})
	go func() {
		server.Handler(authentication).ServeHTTP(response, request)
		close(done)
	}()
	awaitSummaryRead(t, history)
	awaitFollowCall(t, follower)
	stream := awaitFollowStream(t, follower)
	stream.send(&historypb.FollowSessionResponse{ThroughCursor: 5, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
	awaitSummaryRead(t, history)
	if strings.Contains(response.String(), "event: end") {
		t.Fatalf("terminal feed ended before its committed cursor was covered: %s", response.String())
	}
	updates.changes <- struct{}{}
	awaitSummaryRead(t, history)
	if strings.Contains(response.String(), "event: end") {
		t.Fatalf("summary wake ended before the History stream caught up: %s", response.String())
	}
	entry := &eventlogpb.EventEntry{
		Cursor: 6,
		Event: &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{CommandAdmitted: &eventpb.CommandAdmitted{
			Command: &commandpb.Command{CommandId: "wait-for-this"},
		}}},
	}
	stream.send(&historypb.FollowSessionResponse{ThroughCursor: 6, Frame: &historypb.FollowSessionResponse_Entries{Entries: &historypb.Entries{Entries: []*eventlogpb.EventEntry{entry}}}})
	stream.send(&historypb.FollowSessionResponse{ThroughCursor: 6, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
	waitForStreamText(t, response, "event: end")
	body := response.String()
	if strings.Index(body, "id: 6") > strings.Index(body, "event: end") || strings.Contains(body, "event: error") {
		t.Fatalf("awaited admission or terminal event order = %s", body)
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("thread event stream did not stop after the terminal feed frame")
	}
}

func TestThreadEventStreamWaitsForCommittedCursorCoverage(t *testing.T) {
	authentication := newStreamTestAuth(t)
	threadID := uuid.New()
	history := &streamTestHistory{
		feedSummary: historydata.FoldSummary{
			CommittedCursor: 6,
			Feed: &historydata.FeedSnapshot{
				Status:   historypb.FeedStatus_FEED_STATUS_ENDED,
				Attached: &sessionpb.Attached{SessionId: threadID.String(), LastCursor: 6},
				End:      &historydata.FeedEnd{},
			},
		},
		summaryRead: make(chan struct{}, 8),
	}
	follower := newThreadFollowClient(threadFollowScript{})
	server := NewHTTPServer(authentication)
	RegisterStreamingRoutes(server, StreamingDependencies{
		Threads: &streamTestThreadStore{row: &threads.Thread{ID: threadID}},
		History: history, HistoryFollower: follower, Updates: &streamTestChanges{changes: make(chan struct{}, 1)},
	})
	request := httptest.NewRequest(http.MethodGet, "/threads/"+threadID.String()+"/events/stream", nil)
	request.Header.Set("Authorization", "Bearer accepted")
	response := newStreamingRecorder()
	done := make(chan struct{})
	go func() {
		server.Handler(authentication).ServeHTTP(response, request)
		close(done)
	}()
	waitForStreamText(t, response, "event: attached")
	awaitSummaryRead(t, history)
	if strings.Contains(response.String(), "event: end") {
		t.Fatalf("terminal feed ended before its committed cursor was covered: %s", response.String())
	}
	awaitFollowCall(t, follower)
	stream := awaitFollowStream(t, follower)
	stream.send(&historypb.FollowSessionResponse{ThroughCursor: 5, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
	awaitSummaryRead(t, history)
	if strings.Contains(response.String(), "event: end") {
		t.Fatalf("terminal feed ended before FollowSession covered its committed cursor: %s", response.String())
	}
	stream.send(&historypb.FollowSessionResponse{ThroughCursor: 6, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
	waitForStreamText(t, response, "event: end")
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("thread event stream did not stop after the terminal feed frame")
	}
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

func waitForStreamTextAfter(t *testing.T, response *streamingRecorder, before, after string) {
	t.Helper()
	deadline := time.After(2 * time.Second)
	for {
		body := response.String()
		if index := strings.Index(body, before); index >= 0 && strings.Contains(body[index+len(before):], after) {
			return
		}
		select {
		case <-response.written:
		case <-deadline:
			t.Fatalf("stream did not contain %q after %q; body: %s", after, before, body)
		}
	}
}

const liveFramesSandboxUID = "opaque-kubernetes-uid"

// countingLive is a live inventory whose snapshot the test replaces. It counts reads and signals every subscriber
// of each change.
type countingLive struct {
	mu           sync.Mutex
	snapshot     liveinventory.Snapshot
	reads        int
	subscribers  []chan struct{}
	unsubscribed chan struct{}
}

func (f *countingLive) Snapshot(context.Context) (liveinventory.Snapshot, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reads++
	return f.snapshot, nil
}

func (f *countingLive) Health() watchhealth.Status {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.snapshot.Watch
}

func (f *countingLive) Subscribe() (<-chan struct{}, func()) {
	f.mu.Lock()
	defer f.mu.Unlock()
	changes := make(chan struct{}, 1)
	f.subscribers = append(f.subscribers, changes)
	return changes, func() { f.unsubscribed <- struct{}{} }
}

func (f *countingLive) change(snapshot liveinventory.Snapshot) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.snapshot = snapshot
	for _, changes := range f.subscribers {
		select {
		case changes <- struct{}{}:
		default:
		}
	}
}

func (f *countingLive) readCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.reads
}

func liveFramesSnapshot() liveinventory.Snapshot {
	return liveinventory.Snapshot{Watch: watchhealth.Status{Available: true, Synced: true}}
}

func testEnvironment(namespace, name, uid, accountNamespace, accountName string) *environmentpb.EnvironmentDescriptor {
	return &environmentpb.EnvironmentDescriptor{
		Target:           &environmentpb.TargetHandle{Namespace: namespace, Name: name, Uid: uid},
		ExecutionAccount: &environmentpb.AccountHandle{Namespace: accountNamespace, Name: accountName},
	}
}

type liveFramesFixture struct {
	live   *countingLive
	envs   *streamTestEnvironments
	routes *streamingRoutes
}

func newLiveFramesFixture() *liveFramesFixture {
	f := &liveFramesFixture{
		live: &countingLive{snapshot: liveFramesSnapshot(), unsubscribed: make(chan struct{}, 8)},
		envs: &streamTestEnvironments{descriptor: testEnvironment("tools", "demo", liveFramesSandboxUID, "tools", "demo"), changes: make(chan struct{}, 1)},
	}
	f.routes = newStreamingRoutes(NewHTTPServer(nil), StreamingDependencies{
		SandboxNamespace: "tools", Live: f.live, Sandboxes: f.envs,
	})
	return f
}

// openLiveStreams serves count identical requests and returns their responses and a wait for every handler to return.
func openLiveStreams(ctx context.Context, handler http.HandlerFunc, newRequest func() *http.Request, count int) ([]*streamingRecorder, func()) {
	var handlers sync.WaitGroup
	responses := make([]*streamingRecorder, count)
	for n := range responses {
		responses[n] = newStreamingRecorder()
		request := newRequest().WithContext(ctx)
		handlers.Go(func() { handler(responses[n], request) })
	}
	return responses, handlers.Wait
}

func waitForEveryStream(t *testing.T, responses []*streamingRecorder, text string) {
	t.Helper()
	for _, response := range responses {
		waitForStreamText(t, response, text)
	}
}

func TestLiveSandboxStreamsSharePolicyReadsAcrossViewers(t *testing.T) {
	f := newLiveFramesFixture()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	responses, wait := openLiveStreams(ctx, f.routes.liveSandbox, func() *http.Request {
		request := httptest.NewRequest(http.MethodGet, "/live/sandboxes/demo?sandbox_uid="+liveFramesSandboxUID, nil)
		request.SetPathValue("name", "demo")
		return request
	}, 2)
	waitForEveryStream(t, responses, `"sandbox_uid":"`+liveFramesSandboxUID+`"`)
	if got := f.live.readCount(); got != 1 {
		t.Fatalf("initial shared reads = %d, want 1", got)
	}
	for _, response := range responses {
		for _, line := range strings.Split(response.String(), "\n") {
			if !strings.HasPrefix(line, "data: ") {
				continue
			}
			var frame map[string]json.RawMessage
			if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &frame); err != nil {
				t.Fatal(err)
			}
			if len(frame) != 4 || frame["sandbox_uid"] == nil || frame["bindings"] == nil || frame["action_policy"] == nil || frame["watch"] == nil {
				t.Fatalf("policy composition frame has unexpected fields: %s", line)
			}
		}
	}
	changed := liveFramesSnapshot()
	changed.Watch.Failing = map[watchhealth.Kind]watchhealth.Failure{"policysets": {Error: "test failure", Since: time.Date(2026, 10, 20, 1, 4, 3, 0, time.UTC)}}
	f.live.change(changed)
	waitForEveryStream(t, responses, `"policysets":{"error":"test failure"`)
	const replacementUID = "44444444-4444-4444-8444-444444444444"
	f.envs.set(testEnvironment("tools", "demo", replacementUID, "tools", "demo"))
	f.envs.changes <- struct{}{}
	waitForEveryStream(t, responses, `"sandbox_uid":null`)
	for _, response := range responses {
		if strings.Contains(response.String(), replacementUID) {
			t.Fatal("stream followed a same-name replacement")
		}
	}
	cancel()
	wait()
	select {
	case <-f.live.unsubscribed:
	case <-time.After(2 * time.Second):
		t.Fatal("shared producer did not stop")
	}
}

type scriptedEnvironmentWatch struct {
	streamSandboxInventory
	observations chan sandboxes.EnvironmentObservation
	target       *environmentpb.TargetHandle
}

func (f *scriptedEnvironmentWatch) Watch(_ context.Context, target *environmentpb.TargetHandle) <-chan sandboxes.EnvironmentObservation {
	f.target = proto.Clone(target).(*environmentpb.TargetHandle)
	return f.observations
}

func TestSandboxPolicyWatchRejectsMismatchedTargetsAndTerminatesOnErrors(t *testing.T) {
	for _, test := range []struct {
		name        string
		observation sandboxes.EnvironmentObservation
		wantCode    codes.Code
		wantMissing bool
	}{
		{name: "namespace", observation: sandboxes.EnvironmentObservation{Descriptor: testEnvironment("other", "demo", liveFramesSandboxUID, "tools", "demo")}, wantCode: codes.Unknown},
		{name: "name", observation: sandboxes.EnvironmentObservation{Descriptor: testEnvironment("tools", "other", liveFramesSandboxUID, "tools", "demo")}, wantCode: codes.Unknown},
		{name: "uid", observation: sandboxes.EnvironmentObservation{Descriptor: testEnvironment("tools", "demo", "replacement-uid", "tools", "demo")}, wantCode: codes.Unknown},
		{name: "empty", observation: sandboxes.EnvironmentObservation{}, wantCode: codes.Unknown},
		{name: "missing", observation: sandboxes.EnvironmentObservation{Err: status.Error(codes.NotFound, "Environment no longer exists")}, wantCode: codes.OK, wantMissing: true},
		{name: "unavailable", observation: sandboxes.EnvironmentObservation{Err: status.Error(codes.Unavailable, "watch disconnected")}, wantCode: codes.Unavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			watcher := &scriptedEnvironmentWatch{observations: make(chan sandboxes.EnvironmentObservation, 1)}
			watcher.observations <- test.observation
			close(watcher.observations)
			live := &countingLive{snapshot: liveFramesSnapshot(), unsubscribed: make(chan struct{}, 1)}
			routes := newStreamingRoutes(NewHTTPServer(nil), StreamingDependencies{SandboxNamespace: "tools", Live: live, Sandboxes: watcher})
			var frames []sandboxDetail
			err := routes.produceSandboxDetail(t.Context(), sandboxDetailKey{name: "demo", uid: liveFramesSandboxUID}, func(frame sandboxDetail) { frames = append(frames, frame) })
			if status.Code(err) != test.wantCode {
				t.Fatalf("watch result = %v, want code %v", err, test.wantCode)
			}
			if !proto.Equal(watcher.target, &environmentpb.TargetHandle{Namespace: "tools", Name: "demo", Uid: liveFramesSandboxUID}) {
				t.Fatalf("watch target = %v", watcher.target)
			}
			if test.wantMissing {
				if len(frames) != 1 || frames[0].sandboxUID != nil || frames[0].subject != nil || !frames[0].watch.Healthy {
					t.Fatalf("missing Environment frames = %#v", frames)
				}
			} else if len(frames) != 0 {
				t.Fatalf("failed watch published policy frames: %#v", frames)
			}
		})
	}
}

type blockingDecisionWatch struct {
	requests chan *egress_adminpb.WatchDecisionsRequest
	stopped  chan struct{}
}

func (f *blockingDecisionWatch) WatchDecisions(ctx context.Context, request *egress_adminpb.WatchDecisionsRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[egress_adminpb.WatchDecisionsResponse], error) {
	f.requests <- request
	return &blockingDecisionStream{ctx: ctx, stopped: f.stopped}, nil
}

type blockingDecisionStream struct {
	grpc.ServerStreamingClient[egress_adminpb.WatchDecisionsResponse]
	ctx     context.Context
	stopped chan struct{}
}

func (f *blockingDecisionStream) Recv() (*egress_adminpb.WatchDecisionsResponse, error) {
	<-f.ctx.Done()
	close(f.stopped)
	return nil, status.FromContextError(f.ctx.Err()).Err()
}

func TestEgressDecisionWatchStopsWhenEnvironmentIsReplaced(t *testing.T) {
	live := &streamTestLive{snapshots: []liveinventory.Snapshot{{}}, changes: make(chan struct{})}
	environments := &streamTestEnvironments{descriptor: testEnvironment("tools", "demo", liveFramesSandboxUID, "tools", "account"), changes: make(chan struct{}, 1)}
	watcher := &blockingDecisionWatch{requests: make(chan *egress_adminpb.WatchDecisionsRequest, 2), stopped: make(chan struct{})}
	routes := newStreamingRoutes(NewHTTPServer(nil), StreamingDependencies{SandboxNamespace: "tools", Live: live, Sandboxes: environments, Egress: watcher})
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	request := httptest.NewRequest(http.MethodGet, "/live/sandboxes/demo/egress/decisions?sandbox_uid="+liveFramesSandboxUID, nil).WithContext(ctx)
	request.SetPathValue("name", "demo")
	response := newStreamingRecorder()
	done := make(chan struct{})
	go func() {
		routes.liveDecisions(response, request)
		close(done)
	}()
	select {
	case opened := <-watcher.requests:
		if opened.GetFilter().GetSubject().GetNamespace() != "tools" || opened.GetFilter().GetSubject().GetName() != "account" {
			t.Fatalf("decision subject = %v", opened.GetFilter().GetSubject())
		}
	case <-time.After(time.Second):
		t.Fatal("decision watch did not open")
	}
	environments.set(testEnvironment("tools", "demo", "replacement-uid", "tools", "replacement-account"))
	environments.changes <- struct{}{}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("decision route did not end after Environment replacement")
	}
	select {
	case <-watcher.stopped:
	case <-time.After(time.Second):
		t.Fatal("Environment replacement did not cancel Egress watch")
	}
	select {
	case opened := <-watcher.requests:
		t.Fatalf("decision watch reopened after replacement: %v", opened)
	default:
	}
}

func TestEnvironmentFacadeRequiresAppCallerAndForwardsManagementAndWatch(t *testing.T) {
	authentication := newStreamTestAuth(t)
	server := NewHTTPServer(authentication)
	var gotPath, gotMethod, gotQuery string
	upstream := &streamTestUpstream{forward: func(w http.ResponseWriter, request *http.Request, _ context.Context, path, bearer string) error {
		gotPath, gotMethod, gotQuery = path, request.Method, request.URL.RawQuery
		if bearer != "" {
			t.Fatalf("unexpected forwarded bearer: %q", bearer)
		}
		w.WriteHeader(http.StatusOK)
		return nil
	}}
	RegisterStreamingRoutes(server, StreamingDependencies{EnvironmentHTTP: upstream})
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
		{http.MethodPost, "/upstream/environments/v1/environments", "v1/environments", ""},
		{http.MethodDelete, "/upstream/environments/v1/environments/test-envs/workspace?target.uid=selected-uid", "v1/environments/test-envs/workspace", "target.uid=selected-uid"},
		{http.MethodPatch, "/upstream/environments/v1/environments/test-envs/workspace:idle-suspend", "v1/environments/test-envs/workspace:idle-suspend", ""},
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
	server := NewHTTPServer(authentication)
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
	RegisterStreamingRoutes(server, StreamingDependencies{EnvironmentHTTP: upstream})
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
	server := NewHTTPServer(authentication)
	generated, err := facade.NewHandler(t.Context(), "127.0.0.1:1", time.Second, environmentpb.RegisterEnvironmentServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	RegisterStreamingRoutes(server, StreamingDependencies{EnvironmentHTTP: proxy.NewHandlerUpstream(generated, "")})
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
	server := NewHTTPServer(authentication)
	started := make(chan context.Context, 1)
	canceled := make(chan error, 1)
	upstream := &streamTestUpstream{forward: func(_ http.ResponseWriter, _ *http.Request, ctx context.Context, _, _ string) error {
		started <- ctx
		<-ctx.Done()
		canceled <- ctx.Err()
		return ctx.Err()
	}}
	RegisterStreamingRoutes(server, StreamingDependencies{EnvironmentHTTP: upstream})
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

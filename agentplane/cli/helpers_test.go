package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/structpb"
)

const (
	testCredential = "agentplane-credential-test-placeholder"
	missingID      = "00000000-0000-0000-0000-00000000dead"
)

type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// cli is one invocation of the command with a fixed environment.
type cli struct {
	stdout, stderr syncBuffer
	app            *app
}

func newCLI(env map[string]string, stdin string) *cli {
	c := &cli{}
	c.app = newApp(func(name string) string { return env[name] }, strings.NewReader(stdin), &c.stdout, &c.stderr)
	return c
}

// run executes the command line and returns the exit status.
func (c *cli) run(args ...string) int {
	return c.app.execute(args)
}

// invoke runs one command line and returns its exit status, stdout and stderr.
func invoke(env map[string]string, args ...string) (int, string, string) {
	c := newCLI(env, "")
	code := c.run(args...)
	return code, c.stdout.String(), c.stderr.String()
}

func actionsEnv(baseURL string) map[string]string {
	return map[string]string{"AGENTPLANE_ACTIONS_URL": baseURL, "AGENTPLANE_ACTIONS_CREDENTIAL": testCredential}
}

func notificationsEnv(baseURL string) map[string]string {
	return map[string]string{
		"AGENTPLANE_NOTIFICATIONS_URL":        baseURL,
		"AGENTPLANE_NOTIFICATIONS_CREDENTIAL": testCredential,
		"AGENTPLANE_DESTINATION":              testDestination,
	}
}

func egressEnv(baseURL string) map[string]string {
	return map[string]string{"AGENTPLANE_EGRESS_URL": baseURL, "AGENTPLANE_EGRESS_CREDENTIAL": testCredential}
}

const testDestination = `{"destination_ref": {"namespace": "test-ns", "name": "test-sandbox", "uid": "test-uid"}, "session_id": "test-session"}`

// seen is a request a fake service received.
type seen struct {
	method, path  string
	query         url.Values
	body          []byte
	authorization string
}

// fake is a service that records what it was asked and answers with handler.
type fake struct {
	*httptest.Server
	mu   sync.Mutex
	seen []seen
}

func newFake(t *testing.T, handler func(w http.ResponseWriter, r *http.Request, s seen)) *fake {
	t.Helper()
	f := &fake{}
	f.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("reading request body: %v", err)
		}
		s := seen{r.Method, r.URL.Path, r.URL.Query(), body, r.Header.Get("Authorization")}
		f.mu.Lock()
		f.seen = append(f.seen, s)
		f.mu.Unlock()
		handler(w, r, s)
	}))
	t.Cleanup(f.Close)
	return f
}

func (f *fake) all() []seen {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.seen)
}

// requests are the recorded requests with this method.
func (f *fake) requests(method string) []seen {
	var matching []seen
	for _, s := range f.all() {
		if s.method == method {
			matching = append(matching, s)
		}
	}
	return matching
}

func (f *fake) count() int {
	return len(f.all())
}

func reply(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = io.WriteString(w, body)
}

// drop ends the connection without answering: the request arrived and the response is lost.
func drop(w http.ResponseWriter) {
	connection, _, err := w.(http.Hijacker).Hijack()
	if err == nil {
		_ = connection.Close()
	}
}

func decodeObject(t *testing.T, text string) map[string]any {
	t.Helper()
	var object map[string]any
	if err := json.Unmarshal([]byte(text), &object); err != nil {
		t.Fatalf("not a JSON object: %q: %v", text, err)
	}
	return object
}

// errorOf is the error envelope on the last line of stderr.
func errorOf(t *testing.T, stderr string) apiError {
	t.Helper()
	lines := strings.Split(strings.TrimSpace(stderr), "\n")
	var parsed envelope
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &parsed); err != nil {
		t.Fatalf("stderr does not end in an error envelope: %q: %v", stderr, err)
	}
	return parsed.Error
}

func jsonLines(t *testing.T, stdout string) []map[string]any {
	t.Helper()
	var objects []map[string]any
	for line := range strings.SplitSeq(strings.TrimSpace(stdout), "\n") {
		if line != "" {
			objects = append(objects, decodeObject(t, line))
		}
	}
	return objects
}

type actionCallRecord struct {
	method        string
	authorization []string
}

type actionFake struct {
	action_servicepb.UnimplementedActionServiceServer
	mu    sync.Mutex
	calls []actionCallRecord

	listGroups    func(context.Context, *action_servicepb.ListActionGroupsRequest) (*action_servicepb.ListActionGroupsResponse, error)
	getAction     func(context.Context, *action_servicepb.GetActionRequest) (*action_servicepb.GetActionResponse, error)
	getPolicy     func(context.Context, *action_servicepb.GetActionPolicyRequest) (*action_servicepb.GetActionPolicyResponse, error)
	submit        func(context.Context, *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error)
	listRequests  func(context.Context, *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error)
	getRequest    func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error)
	watchRequest  func(*action_servicepb.WatchActionRequestRequest, grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error
	cancelRequest func(context.Context, *action_servicepb.CancelActionRequestRequest) (*action_servicepb.CancelActionRequestResponse, error)
	listEvents    func(context.Context, *action_servicepb.ListActionRequestEventsRequest) (*action_servicepb.ListActionRequestEventsResponse, error)
}

func newActionFake(t *testing.T) (*actionFake, string) {
	t.Helper()
	for _, variable := range []string{"HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"} {
		t.Setenv(variable, "")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	fake := &actionFake{}
	action_servicepb.RegisterActionServiceServer(server, fake)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return fake, "http://" + listener.Addr().String()
}

func (s *actionFake) record(ctx context.Context, method string) {
	md, _ := metadata.FromIncomingContext(ctx)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls = append(s.calls, actionCallRecord{method: method, authorization: md.Get("authorization")})
}

func (s *actionFake) callLog() []actionCallRecord {
	s.mu.Lock()
	defer s.mu.Unlock()
	return slices.Clone(s.calls)
}

func (s *actionFake) count(method string) int {
	count := 0
	for _, call := range s.callLog() {
		if call.method == method {
			count++
		}
	}
	return count
}

func (s *actionFake) ListActionGroups(ctx context.Context, request *action_servicepb.ListActionGroupsRequest) (*action_servicepb.ListActionGroupsResponse, error) {
	s.record(ctx, "ListActionGroups")
	if s.listGroups != nil {
		return s.listGroups(ctx, request)
	}
	return &action_servicepb.ListActionGroupsResponse{}, nil
}

func (s *actionFake) GetAction(ctx context.Context, request *action_servicepb.GetActionRequest) (*action_servicepb.GetActionResponse, error) {
	s.record(ctx, "GetAction")
	if s.getAction != nil {
		return s.getAction(ctx, request)
	}
	return nil, status.Error(codes.NotFound, "Action not found")
}

func (s *actionFake) GetActionPolicy(ctx context.Context, request *action_servicepb.GetActionPolicyRequest) (*action_servicepb.GetActionPolicyResponse, error) {
	s.record(ctx, "GetActionPolicy")
	if s.getPolicy != nil {
		return s.getPolicy(ctx, request)
	}
	return &action_servicepb.GetActionPolicyResponse{ActionPolicy: &action_servicepb.CallerActionPolicy{}}, nil
}

func (s *actionFake) SubmitActionRequest(ctx context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
	s.record(ctx, "SubmitActionRequest")
	if s.submit != nil {
		return s.submit(ctx, request)
	}
	return &action_servicepb.SubmitActionRequestResponse{ActionRequest: testActionRequest("test-request-id", action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING)}, nil
}

func (s *actionFake) ListActionRequests(ctx context.Context, request *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
	s.record(ctx, "ListActionRequests")
	if s.listRequests != nil {
		return s.listRequests(ctx, request)
	}
	return &action_servicepb.ListActionRequestsResponse{}, nil
}

func (s *actionFake) GetActionRequest(ctx context.Context, request *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
	s.record(ctx, "GetActionRequest")
	if s.getRequest != nil {
		return s.getRequest(ctx, request)
	}
	return nil, status.Error(codes.NotFound, "Action request not found")
}

func (s *actionFake) WatchActionRequest(request *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
	s.record(stream.Context(), "WatchActionRequest")
	if s.watchRequest != nil {
		return s.watchRequest(request, stream)
	}
	return status.Error(codes.Unimplemented, "WatchActionRequest not configured")
}

func (s *actionFake) CancelActionRequest(ctx context.Context, request *action_servicepb.CancelActionRequestRequest) (*action_servicepb.CancelActionRequestResponse, error) {
	s.record(ctx, "CancelActionRequest")
	if s.cancelRequest != nil {
		return s.cancelRequest(ctx, request)
	}
	return &action_servicepb.CancelActionRequestResponse{
		Outcome:       action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_CANCELLED,
		ActionRequest: testActionRequest(request.GetRequestId(), action_servicepb.ActionState_ACTION_STATE_CANCELLED),
	}, nil
}

func (s *actionFake) ListActionRequestEvents(ctx context.Context, request *action_servicepb.ListActionRequestEventsRequest) (*action_servicepb.ListActionRequestEventsResponse, error) {
	s.record(ctx, "ListActionRequestEvents")
	if s.listEvents != nil {
		return s.listEvents(ctx, request)
	}
	return &action_servicepb.ListActionRequestEventsResponse{}, nil
}

func testActionRequest(id string, state action_servicepb.ActionState) *action_servicepb.ActionRequest {
	return &action_servicepb.ActionRequest{
		Id: id, IdempotencyKey: "test-idempotency-key",
		Action:    &action_servicepb.ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments: &structpb.Struct{Fields: map[string]*structpb.Value{}},
		Title:     "test action title", State: state, Version: 1,
	}
}

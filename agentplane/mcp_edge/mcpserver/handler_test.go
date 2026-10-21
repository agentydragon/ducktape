package mcpserver

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

const testRequestID = "00000000-0000-0000-0000-0000000000a1"

func testRequest(state pb.ActionState) *pb.ActionRequest {
	at := timestamppb.New(time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC))
	return &pb.ActionRequest{
		Id:             testRequestID,
		IdempotencyKey: "test-key",
		Action:         &pb.ActionIdentity{Group: "test-mcp", Name: "alpha"},
		Arguments:      &structpb.Struct{},
		Title:          "test title",
		Caller:         &sandboxes.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"},
		State:          state,
		Version:        1,
		CreatedAt:      at,
		UpdatedAt:      at,
	}
}

var testCatalog = &pb.ListActionGroupsResponse{ActionGroups: []*pb.ActionGroup{{
	Key:          "test-mcp",
	ExecutorKind: pb.ExecutorKind_EXECUTOR_KIND_MCP,
	Available:    true,
	Actions:      []*pb.Action{{Group: "test-mcp", Name: "alpha", Description: "test-alpha"}},
}}}

// recordingCore answers from answers, by RPC name, and records the RPCs and the callers they arrived for.
type recordingCore struct {
	mu      sync.Mutex
	rpcs    []string
	callers []string
}

func (r *recordingCore) record(ctx context.Context, rpc string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.rpcs = append(r.rpcs, rpc)
	r.callers = append(r.callers, callerOf(ctx))
}

func (r *recordingCore) recorded() ([]string, []string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return slices.Clone(r.rpcs), slices.Clone(r.callers)
}

func (r *recordingCore) core(
	answers map[string]proto.Message,
	watch func(*pb.WatchActionRequestRequest, grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error,
) *fakeCore {
	return &fakeCore{
		unary: func(ctx context.Context, rpc string, _ proto.Message) (proto.Message, error) {
			r.record(ctx, rpc)
			if answer, ok := answers[rpc]; ok {
				return answer, nil
			}
			return nil, status.Error(codes.Unimplemented, "test core has no answer")
		},
		watch: func(request *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
			r.record(stream.Context(), "WatchActionRequest")
			return watch(request, stream)
		},
	}
}

func noWatch(*pb.WatchActionRequestRequest, grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
	return status.Error(codes.Unimplemented, "test core has no watch")
}

func post(t *testing.T, url string, headers http.Header, body string) *http.Response {
	t.Helper()
	request, err := http.NewRequestWithContext(t.Context(), http.MethodPost, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Header = headers
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = response.Body.Close() })
	return response
}

// mcpHeaders are what a Streamable HTTP client sends with a JSON-RPC message.
func mcpHeaders(authorization ...string) http.Header {
	headers := http.Header{
		"Content-Type":         {"application/json"},
		"Accept":               {"application/json, text/event-stream"},
		"Mcp-Protocol-Version": {"2025-11-25"},
	}
	for _, value := range authorization {
		headers.Add("Authorization", value)
	}
	return headers
}

const toolsList = `{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}`

func TestRefusedBearersGetFastMCPsChallenges(t *testing.T) {
	h := newHarness(t, &fakeCore{}, 30)
	resourceMetadata := `resource_metadata="https://test-actions.example/.well-known/oauth-protected-resource/mcp"`
	initialize := `{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", ` +
		`"capabilities": {}, "clientInfo": {"name": "test-client", "version": "0"}}}`
	for _, tc := range []struct {
		name          string
		method        string
		body          string
		authorization []string
		status        int
		// invalidToken is whether the refusal names the presented token invalid (RFC 6750 §3.1).
		invalidToken bool
	}{
		{name: "no bearer", method: http.MethodPost, body: toolsList, status: 401},
		{name: "no bearer on initialize", method: http.MethodPost, body: initialize, status: 401},
		{name: "no bearer on delete", method: http.MethodDelete, status: 401},
		{name: "revoked bearer", method: http.MethodPost, body: toolsList, authorization: []string{"Bearer " + revokedToken}, status: 401, invalidToken: true},
		{name: "revoked bearer on delete", method: http.MethodDelete, authorization: []string{"Bearer " + revokedToken}, status: 401, invalidToken: true},
		{name: "not a bearer", method: http.MethodPost, body: toolsList, authorization: []string{"Basic dGVzdDp0ZXN0"}, status: 401, invalidToken: true},
		{
			name: "two bearers", method: http.MethodPost, body: toolsList,
			authorization: []string{"Bearer " + callerToken, "Bearer " + otherCallerToken}, status: 401, invalidToken: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			request, err := http.NewRequestWithContext(t.Context(), tc.method, h.url(), strings.NewReader(tc.body))
			if err != nil {
				t.Fatal(err)
			}
			request.Header = mcpHeaders(tc.authorization...)
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = response.Body.Close() }()
			if response.StatusCode != tc.status {
				t.Fatalf("status = %d, want %d", response.StatusCode, tc.status)
			}
			challenge := response.Header.Get("WWW-Authenticate")
			if !strings.HasPrefix(challenge, "Bearer") || !strings.Contains(challenge, resourceMetadata) {
				t.Errorf("challenge = %q, want a bearer challenge naming the resource metadata", challenge)
			}
			if got := strings.Contains(challenge, `error="invalid_token"`); got != tc.invalidToken {
				t.Errorf("challenge = %q, invalid_token named = %v, want %v", challenge, got, tc.invalidToken)
			}
			body, err := io.ReadAll(response.Body)
			if err != nil {
				t.Fatal(err)
			}
			if !tc.invalidToken {
				if len(body) != 0 {
					t.Errorf("body = %q, want none", body)
				}
				return
			}
			var refusal struct {
				Error string `json:"error"`
			}
			if err := json.Unmarshal(body, &refusal); err != nil || refusal.Error != "invalid_token" {
				t.Errorf("body = %q, want an invalid_token error", body)
			}
		})
	}
}

func TestAnAuthorityOutageIsNotAnInvalidToken(t *testing.T) {
	h := newHarness(t, &fakeCore{}, 30)
	response := post(t, h.url(), mcpHeaders("Bearer "+outageToken), toolsList)
	if response.StatusCode != http.StatusServiceUnavailable || response.Header.Get("Retry-After") == "" {
		t.Errorf("status = %d, Retry-After = %q; want 503 with a Retry-After", response.StatusCode,
			response.Header.Get("Retry-After"))
	}
	if challenge := response.Header.Get("WWW-Authenticate"); challenge != "" {
		t.Errorf("challenge = %q, want none", challenge)
	}
	if broken := post(t, h.url(), mcpHeaders("Bearer "+brokenToken), toolsList); broken.StatusCode != http.StatusInternalServerError {
		t.Errorf("a verifier fault answered %d, want 500", broken.StatusCode)
	}
}

func TestOnlyPostAndDeleteReachTheEndpoint(t *testing.T) {
	h := newHarness(t, &fakeCore{}, 30)
	request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, h.url(), nil)
	if err != nil {
		t.Fatal(err)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("GET answered %d, want 405", response.StatusCode)
	}
}

func TestLoopbackGuardRefusesForeignOriginsAndHosts(t *testing.T) {
	h := newHarness(t, &fakeCore{}, 30)
	for _, tc := range []struct {
		name   string
		header string
		value  string
		status int
	}{
		{name: "loopback origin", header: "Origin", value: "http://127.0.0.1", status: http.StatusOK},
		{name: "foreign origin", header: "Origin", value: "https://test-untrusted.example", status: http.StatusForbidden},
		{name: "rebound host", header: "Host", value: "test-rebinding.example", status: http.StatusMisdirectedRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			request, err := http.NewRequestWithContext(t.Context(), http.MethodPost, h.url(), strings.NewReader(toolsList))
			if err != nil {
				t.Fatal(err)
			}
			request.Header = mcpHeaders("Bearer " + callerToken)
			if tc.header == "Host" {
				request.Host = tc.value
			} else {
				request.Header.Set(tc.header, tc.value)
			}
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = response.Body.Close() }()
			if response.StatusCode != tc.status {
				t.Errorf("status = %d, want %d", response.StatusCode, tc.status)
			}
		})
	}
}

func TestToolsCallTheCoreAsTheVerifiedCaller(t *testing.T) {
	recorder := &recordingCore{}
	h := newHarness(t, recorder.core(map[string]proto.Message{
		"GetActionPolicy": &pb.GetActionPolicyResponse{ActionPolicy: &pb.CallerActionPolicy{Synced: true}},
	}, func(_ *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
		return stream.Send(&pb.WatchActionRequestResponse{ActionRequest: testRequest(pb.ActionState_ACTION_STATE_DENIED)})
	}), 30)
	for _, token := range []string{callerToken, otherCallerToken} {
		session := h.connect(t, token)
		callTool(t, session, "get_action_policy", `{}`)
		callTool(t, session, "get_action_request", `{"request_id": "`+testRequestID+`", "wait": {"wait_seconds": 10}}`)
	}
	rpcs, callers := recorder.recorded()
	wantRPCs := []string{"GetActionPolicy", "WatchActionRequest", "GetActionPolicy", "WatchActionRequest"}
	wantCallers := []string{"test-caller", "test-caller", "test-other-caller", "test-other-caller"}
	if !slices.Equal(rpcs, wantRPCs) || !slices.Equal(callers, wantCallers) {
		t.Errorf("core saw %v as %v, want %v as %v", rpcs, callers, wantRPCs, wantCallers)
	}
}

func TestInvalidArgumentsAreRefusedBeforeTheCore(t *testing.T) {
	recorder := &recordingCore{}
	session := newHarness(t, recorder.core(nil, noWatch), 30).connect(t, callerToken)
	request := `{"idempotency_key": "test-key", "title": "test title", "action": {"group": "test-mcp", "name": "alpha"}, ` +
		`"arguments": {}`
	for _, tc := range []struct{ name, tool, arguments string }{
		{"forged caller", "request_action", `{"request": ` + request + `, "caller_principal": "test-other"}}`},
		{"negative wait", "request_action", `{"request": ` + request + `}, "wait": {"wait_seconds": -1}}`},
		{"wait above the limit", "request_action", `{"request": ` + request + `}, "wait": {"wait_seconds": 31}}`},
		{"unknown wait condition", "get_action_result", `{"request_id": "` + testRequestID + `", "wait": {"wait_until": "test-never"}}`},
		{"unsupported Action field", "get_action", `{"group": "test-mcp", "name": "alpha", "include_fields": ["output_schema"]}`},
		{"unknown receipt field", "get_action_request", `{"request_id": "` + testRequestID + `", "include_fields": ["bogus"]}`},
		{"malformed request id", "cancel_action_request", `{"request_id": "test-not-a-uuid"}`},
		{"unknown tool", "test_unknown", `{}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if result := callTool(t, session, tc.tool, tc.arguments); !result.IsError {
				t.Errorf("answer = %v, want an error result", result.Content)
			}
		})
	}
	if rpcs, _ := recorder.recorded(); len(rpcs) != 0 {
		t.Errorf("core saw %v, want nothing", rpcs)
	}
}

// receiptState is the state a receipt answer reports.
func receiptState(t *testing.T, result *mcp.CallToolResult) string {
	t.Helper()
	if result.IsError {
		t.Fatalf("answer = %q, want a receipt", text(t, result))
	}
	var receipt struct {
		State string `json:"state"`
	}
	if err := json.Unmarshal([]byte(text(t, result)), &receipt); err != nil {
		t.Fatal(err)
	}
	return receipt.State
}

func TestAWaitAnswersTheReceiptThatSatisfiesIt(t *testing.T) {
	var watched []*pb.WatchActionRequestRequest
	recorder := &recordingCore{}
	session := newHarness(t, recorder.core(nil, func(request *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
		watched = append(watched, request)
		for _, state := range []pb.ActionState{pb.ActionState_ACTION_STATE_DECISION_PENDING, pb.ActionState_ACTION_STATE_CANCELLED} {
			if err := stream.Send(&pb.WatchActionRequestResponse{ActionRequest: testRequest(state)}); err != nil {
				return err
			}
		}
		return nil
	}), 30).connect(t, callerToken)
	terminal := callTool(t, session, "get_action_request", `{"request_id": "`+testRequestID+`", "wait": {"wait_seconds": 10}}`)
	decided := callTool(t, session, "get_action_request",
		`{"request_id": "`+testRequestID+`", "wait": {"wait_seconds": 10, "wait_until": "decision"}}`)
	if got := receiptState(t, terminal); got != "cancelled" {
		t.Errorf("state = %q, want the last frame's", got)
	}
	receiptState(t, decided)
	want := []*pb.WatchActionRequestRequest{
		{RequestId: testRequestID, Until: pb.WatchUntil_WATCH_UNTIL_TERMINAL},
		{RequestId: testRequestID, Until: pb.WatchUntil_WATCH_UNTIL_DECISION},
	}
	if !slices.EqualFunc(watched, want, func(a, b *pb.WatchActionRequestRequest) bool { return proto.Equal(a, b) }) {
		t.Errorf("watched %v, want %v", watched, want)
	}
}

// blockUntilDone sends the given frames, then holds the watch open until its caller ends it, and closes ended.
func blockUntilDone(ended chan<- struct{}, frames ...*pb.ActionRequest) func(*pb.WatchActionRequestRequest, grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
	return func(_ *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
		defer close(ended)
		for _, frame := range frames {
			if err := stream.Send(&pb.WatchActionRequestResponse{ActionRequest: frame}); err != nil {
				return err
			}
		}
		<-stream.Context().Done()
		return stream.Context().Err()
	}
}

func TestAWaitDeadlineAnswersTheCurrentReceipt(t *testing.T) {
	t.Run("the last frame", func(t *testing.T) {
		ended := make(chan struct{})
		recorder := &recordingCore{}
		session := newHarness(t, recorder.core(nil, blockUntilDone(ended, testRequest(pb.ActionState_ACTION_STATE_RUNNING))), 30).
			connect(t, callerToken)
		result := callTool(t, session, "get_action_request", `{"request_id": "`+testRequestID+`", "wait": {"wait_seconds": 1}}`)
		if got := receiptState(t, result); got != "running" {
			t.Errorf("state = %q, want running", got)
		}
		// The deadline ends the watch.
		<-ended
	})
	t.Run("a read when no frame came", func(t *testing.T) {
		recorder := &recordingCore{}
		session := newHarness(t, recorder.core(map[string]proto.Message{
			"GetActionRequest": &pb.GetActionRequestResponse{ActionRequest: testRequest(pb.ActionState_ACTION_STATE_DECISION_PENDING)},
		}, blockUntilDone(make(chan struct{}))), 30).connect(t, callerToken)
		result := callTool(t, session, "get_action_request", `{"request_id": "`+testRequestID+`", "wait": {"wait_seconds": 0.001}}`)
		// Only the read answers decision_pending.
		if got := receiptState(t, result); got != "decision_pending" {
			t.Errorf("state = %q, want decision_pending", got)
		}
	})
}

func TestAWatchTheCoreEndsEarlyAnswersAsPythonDid(t *testing.T) {
	for _, tc := range []struct {
		code codes.Code
		want toolError
	}{
		{codes.Unavailable, updatesLost},
		{codes.Unauthenticated, authorizationExpired},
		{codes.NotFound, requestNotFound},
	} {
		t.Run(tc.code.String(), func(t *testing.T) {
			recorder := &recordingCore{}
			session := newHarness(t, recorder.core(nil, func(*pb.WatchActionRequestRequest, grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
				return status.Error(tc.code, "test watch ended")
			}), 30).connect(t, callerToken)
			result := callTool(t, session, "get_action_result", `{"request_id": "`+testRequestID+`", "wait": {"wait_seconds": 10}}`)
			if got := text(t, result); !result.IsError || got != string(tc.want) {
				t.Errorf("answer = %q (error %v), want %q", got, result.IsError, tc.want)
			}
		})
	}
}

func TestADisconnectEndsTheWaitWithoutCancellingTheRequest(t *testing.T) {
	watching, ended := make(chan struct{}), make(chan struct{})
	recorder := &recordingCore{}
	h := newHarness(t, recorder.core(map[string]proto.Message{
		"SubmitActionRequest": &pb.SubmitActionRequestResponse{ActionRequest: testRequest(pb.ActionState_ACTION_STATE_DECISION_PENDING)},
	}, func(request *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
		close(watching)
		return blockUntilDone(ended)(request, stream)
	}), 30)
	ctx, disconnect := context.WithCancel(t.Context())
	call := `{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "request_action", "arguments": ` +
		`{"request": {"idempotency_key": "test-disconnect", "title": "test title", "action": {"group": "test-mcp", ` +
		`"name": "alpha"}, "arguments": {}}, "wait": {"wait_seconds": 30}}}}`
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, h.url(), bytes.NewReader([]byte(call)))
	if err != nil {
		t.Fatal(err)
	}
	request.Header = mcpHeaders("Bearer " + callerToken)
	answered := make(chan error, 1)
	go func() {
		response, err := http.DefaultClient.Do(request)
		if err == nil {
			_, err = io.ReadAll(response.Body)
			_ = response.Body.Close()
		}
		answered <- err
	}()
	<-watching
	disconnect()
	<-ended
	<-answered
	if rpcs, _ := recorder.recorded(); !slices.Equal(rpcs, []string{"SubmitActionRequest", "WatchActionRequest"}) {
		t.Errorf("core saw %v, want the submission and its watch alone", rpcs)
	}
}

func TestRequestActionWaitsForTheResultItsToolAnswered(t *testing.T) {
	stored, err := structpb.NewValue(map[string]any{
		"content":           []any{map[string]any{"type": "text", "text": "test answer"}},
		"structuredContent": map[string]any{"width": 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	succeeded := testRequest(pb.ActionState_ACTION_STATE_SUCCEEDED)
	succeeded.Execution = &pb.Execution{
		Id: "00000000-0000-0000-0000-0000000000e1", State: pb.ExecutionState_EXECUTION_STATE_SUCCEEDED, Result: stored,
		CreatedAt: succeeded.GetCreatedAt(),
	}
	recorder := &recordingCore{}
	session := newHarness(t, recorder.core(map[string]proto.Message{
		"SubmitActionRequest": &pb.SubmitActionRequestResponse{ActionRequest: testRequest(pb.ActionState_ACTION_STATE_DECISION_PENDING)},
		"ListActionGroups":    testCatalog,
	}, func(_ *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
		for _, frame := range []*pb.ActionRequest{testRequest(pb.ActionState_ACTION_STATE_ALLOWED), succeeded} {
			if err := stream.Send(&pb.WatchActionRequestResponse{ActionRequest: frame}); err != nil {
				return err
			}
		}
		return nil
	}), 30).connect(t, callerToken)
	result := callTool(t, session, "request_action", `{"request": {"idempotency_key": "test-answered", "title": "test title", `+
		`"action": {"group": "test-mcp", "name": "alpha"}, "arguments": {}}, "wait": {"wait_seconds": 10}}`)
	if result.IsError || text(t, result) != "test answer" {
		t.Errorf("answer = %v (error %v), want the stored one", result.Content, result.IsError)
	}
}

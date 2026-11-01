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

// withoutCatalogRPC removes the request-scoped direct-tool catalog snapshot from assertions about tool operations.
func withoutCatalogRPC(rpcs, callers []string) ([]string, []string) {
	var filteredRPCs, filteredCallers []string
	for i, rpc := range rpcs {
		if rpc != "ListActionGroups" {
			filteredRPCs = append(filteredRPCs, rpc)
			filteredCallers = append(filteredCallers, callers[i])
		}
	}
	return filteredRPCs, filteredCallers
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

func TestRefusedBearersNeverReachTheCore(t *testing.T) {
	recorder := &recordingCore{}
	h := newHarness(t, recorder.core(nil, noWatch), Options{MaxWaitSeconds: 30})
	for _, tc := range []struct {
		name          string
		authorization []string
	}{
		{name: "no bearer"},
		{name: "revoked bearer", authorization: []string{"Bearer " + revokedToken}},
		{name: "two bearers", authorization: []string{"Bearer " + callerToken, "Bearer " + otherCallerToken}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			call := `{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_action_policy", "arguments": {}}}`
			if response := post(t, h.url(), mcpHeaders(tc.authorization...), call); response.StatusCode != http.StatusUnauthorized {
				t.Errorf("status = %d, want 401", response.StatusCode)
			}
		})
	}
	if rpcs, _ := recorder.recorded(); len(rpcs) != 0 {
		t.Errorf("core saw %v, want nothing", rpcs)
	}
}

func TestAnAuthorityOutageIsNotAnInvalidToken(t *testing.T) {
	h := newHarness(t, &fakeCore{}, Options{MaxWaitSeconds: 30})
	response := post(t, h.url(), mcpHeaders("Bearer "+outageToken), toolsList)
	if response.StatusCode != http.StatusServiceUnavailable || response.Header.Get("Retry-After") == "" {
		t.Errorf("status = %d, Retry-After = %q; want 503 with a Retry-After", response.StatusCode,
			response.Header.Get("Retry-After"))
	}
	if challenge := response.Header.Get("WWW-Authenticate"); challenge != "" {
		t.Errorf("challenge = %q, want none", challenge)
	}
	broken := post(t, h.url(), mcpHeaders("Bearer "+brokenToken), toolsList)
	body, err := io.ReadAll(broken.Body)
	if err != nil {
		t.Fatal(err)
	}
	if broken.StatusCode != http.StatusInternalServerError || bytes.Contains(body, []byte("test verifier fault")) {
		t.Errorf("a verifier fault answered %d %q, want 500 without the fault", broken.StatusCode, body)
	}
}

func TestToolsCallTheCoreAsTheVerifiedCaller(t *testing.T) {
	recorder := &recordingCore{}
	h := newHarness(t, recorder.core(map[string]proto.Message{
		"GetActionPolicy": &pb.GetActionPolicyResponse{ActionPolicy: &pb.CallerActionPolicy{Synced: true}},
	}, func(_ *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
		return stream.Send(&pb.WatchActionRequestResponse{ActionRequest: testRequest(pb.ActionState_ACTION_STATE_DENIED)})
	}), Options{MaxWaitSeconds: 30})
	for _, token := range []string{callerToken, otherCallerToken} {
		session := h.connect(t, token)
		callTool(t, session, "get_action_policy", `{}`)
		callTool(t, session, "get_action_request", `{"request_id": "`+testRequestID+`", "wait": {"wait_seconds": 10}}`)
	}
	rpcs, callers := recorder.recorded()
	if slices.ContainsFunc(callers, func(caller string) bool { return caller != "test-caller" && caller != "test-other-caller" }) {
		t.Errorf("core saw an unexpected caller in catalog or tool RPCs: %v as %v", rpcs, callers)
	}
	rpcs, callers = withoutCatalogRPC(rpcs, callers)
	wantRPCs := []string{"GetActionPolicy", "WatchActionRequest", "GetActionPolicy", "WatchActionRequest"}
	wantCallers := []string{"test-caller", "test-caller", "test-other-caller", "test-other-caller"}
	if !slices.Equal(rpcs, wantRPCs) || !slices.Equal(callers, wantCallers) {
		t.Errorf("core saw %v as %v, want %v as %v", rpcs, callers, wantRPCs, wantCallers)
	}
}

func TestInvalidArgumentsAreRefusedBeforeTheCore(t *testing.T) {
	recorder := &recordingCore{}
	session := newHarness(t, recorder.core(nil, noWatch), Options{MaxWaitSeconds: 30}).connect(t, callerToken)
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
	} {
		t.Run(tc.name, func(t *testing.T) {
			if result := callTool(t, session, tc.tool, tc.arguments); !result.IsError {
				t.Errorf("answer = %v, want an error result", result.Content)
			}
		})
	}
	rpcs, callers := recorder.recorded()
	if filtered, _ := withoutCatalogRPC(rpcs, callers); len(filtered) != 0 {
		t.Errorf("core saw non-catalog RPCs in %v", rpcs)
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
	}), Options{MaxWaitSeconds: 30}).connect(t, callerToken)
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

// sendThenFail sends the given frames, then ends the watch with code.
func sendThenFail(code codes.Code, frames ...*pb.ActionRequest) func(*pb.WatchActionRequestRequest, grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
	return func(_ *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
		for _, frame := range frames {
			if err := stream.Send(&pb.WatchActionRequestResponse{ActionRequest: frame}); err != nil {
				return err
			}
		}
		return status.Error(code, "test watch ended")
	}
}

func TestAWaitDeadlineAnswersTheCurrentReceipt(t *testing.T) {
	running := testRequest(pb.ActionState_ACTION_STATE_RUNNING)
	for _, tc := range []struct {
		name  string
		wait  string
		watch func(*pb.WatchActionRequestRequest, grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error
		want  string
	}{
		{"the last frame at the deadline", "1", blockUntilDone(make(chan struct{}), running), "running"},
		// The core sees the deadline as grpc-timeout and can end the watch before this side's clock does.
		{"the last frame when the core ends the wait", "10", sendThenFail(codes.DeadlineExceeded, running), "running"},
		// Only the read answers decision_pending.
		{"a read when no frame came", "0.001", blockUntilDone(make(chan struct{})), "decision_pending"},
		{"a read when the core ends the wait first", "10", sendThenFail(codes.DeadlineExceeded), "decision_pending"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			session := newHarness(t, (&recordingCore{}).core(map[string]proto.Message{
				"GetActionRequest": &pb.GetActionRequestResponse{ActionRequest: testRequest(pb.ActionState_ACTION_STATE_DECISION_PENDING)},
			}, tc.watch), Options{MaxWaitSeconds: 30}).connect(t, callerToken)
			result := callTool(t, session, "get_action_request", `{"request_id": "`+testRequestID+`", "wait": {"wait_seconds": `+tc.wait+`}}`)
			if got := receiptState(t, result); got != tc.want {
				t.Errorf("state = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestAWatchTheCoreEndsEarlyReturnsTheDefinedToolError(t *testing.T) {
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
			}), Options{MaxWaitSeconds: 30}).connect(t, callerToken)
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
	}), Options{MaxWaitSeconds: 30})
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
	rpcs, callers := recorder.recorded()
	if operations, _ := withoutCatalogRPC(rpcs, callers); !slices.Equal(operations, []string{"SubmitActionRequest", "WatchActionRequest"}) {
		t.Errorf("core saw %v, want the submission and its watch apart from catalog snapshots", rpcs)
	}
}

// answeredRequest is a request whose MCP tool answered "test answer".
func answeredRequest(t *testing.T) *pb.ActionRequest {
	t.Helper()
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
	return succeeded
}

func TestSessionDelegationResultIsReturnedAsStructuredContent(t *testing.T) {
	stored, err := structpb.NewValue(map[string]any{
		"dispatch": map[string]any{
			"command_id": "22222222-2222-4222-8222-222222222222",
			"state":      "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	result, err := succeededResult(stored, pb.ExecutorKind_EXECUTOR_KIND_SESSION_DELEGATION)
	if err != nil {
		t.Fatal(err)
	}
	if result.StructuredContent == nil || len(result.Content) != 1 {
		t.Fatalf("result = %+v, want structuredContent plus one text representation", result)
	}
	structured, ok := result.StructuredContent.(json.RawMessage)
	if !ok {
		t.Fatalf("structured content type = %T", result.StructuredContent)
	}
	textContent, ok := result.Content[0].(*mcp.TextContent)
	if !ok || textContent.Text != string(structured) {
		t.Fatalf("text fallback = %T %+v, want the same JSON as structured content", result.Content[0], result.Content[0])
	}
	var decoded map[string]any
	if err := json.Unmarshal(structured, &decoded); err != nil {
		t.Fatal(err)
	}
	dispatch, ok := decoded["dispatch"].(map[string]any)
	if !ok {
		t.Fatalf("structured dispatch = %v", decoded["dispatch"])
	}
	if dispatch["command_id"] != "22222222-2222-4222-8222-222222222222" || dispatch["state"] != "DELIVERY_STATE_QUEUED_IN_DISPATCHER" {
		t.Fatalf("structured result = %v", decoded)
	}
}

func TestRequestActionWaitsForTheResultItsToolAnswered(t *testing.T) {
	succeeded := answeredRequest(t)
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
	}), Options{MaxWaitSeconds: 30}).connect(t, callerToken)
	result := callTool(t, session, "request_action", `{"request": {"idempotency_key": "test-answered", "title": "test title", `+
		`"action": {"group": "test-mcp", "name": "alpha"}, "arguments": {}}, "wait": {"wait_seconds": 10}}`)
	if result.IsError || text(t, result) != "test answer" {
		t.Errorf("answer = %v (error %v), want the stored one", result.Content, result.IsError)
	}
}

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	aclpb "github.com/buildbuddy-io/buildbuddy/proto/acl"
	bbspb "github.com/buildbuddy-io/buildbuddy/proto/buildbuddy_service"
	eventlogpb "github.com/buildbuddy-io/buildbuddy/proto/eventlog"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	invocationstatuspb "github.com/buildbuddy-io/buildbuddy/proto/invocation_status"
	targetpb "github.com/buildbuddy-io/buildbuddy/proto/target"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
)

type invocationWatchTestRPC struct {
	bbspb.BuildBuddyServiceClient
	getInvocation    func(context.Context, *invocationpb.GetInvocationRequest) (*invocationpb.GetInvocationResponse, error)
	getEventLogChunk func(context.Context, *eventlogpb.GetEventLogChunkRequest) (*eventlogpb.GetEventLogChunkResponse, error)
	getTarget        func(context.Context, *targetpb.GetTargetRequest) (*targetpb.GetTargetResponse, error)
}

func (r *invocationWatchTestRPC) GetInvocation(ctx context.Context, req *invocationpb.GetInvocationRequest, _ ...grpc.CallOption) (*invocationpb.GetInvocationResponse, error) {
	return r.getInvocation(ctx, req)
}

func (r *invocationWatchTestRPC) GetEventLogChunk(ctx context.Context, req *eventlogpb.GetEventLogChunkRequest, _ ...grpc.CallOption) (*eventlogpb.GetEventLogChunkResponse, error) {
	return r.getEventLogChunk(ctx, req)
}

func (r *invocationWatchTestRPC) GetTarget(ctx context.Context, req *targetpb.GetTargetRequest, _ ...grpc.CallOption) (*targetpb.GetTargetResponse, error) {
	return r.getTarget(ctx, req)
}

type invocationWatchTestChunk struct {
	id          string
	nextChunkID string
	buffer      string
}

func invocationWatchTestResponse(status invocationstatuspb.InvocationStatus, success bool, firstChunkID string, chunked bool) *invocationpb.GetInvocationResponse {
	return &invocationpb.GetInvocationResponse{
		Invocation: []*invocationpb.Invocation{{
			Acl:                 &aclpb.ACL{GroupId: "test-group"},
			InvocationStatus:    status,
			Success:             success,
			HasChunkedEventLogs: chunked,
			LastChunkId:         firstChunkID,
		}},
	}
}

func invocationWatchTestTargetResponse(failedTest string) *targetpb.GetTargetResponse {
	if failedTest == "" {
		return &targetpb.GetTargetResponse{}
	}
	return &targetpb.GetTargetResponse{
		TargetGroups: []*targetpb.TargetGroup{{
			Targets: []*targetpb.Target{{
				Metadata: &targetpb.TargetMetadata{Label: failedTest},
			}},
		}},
	}
}

func newChunkedInvocationWatchTestClient(
	t *testing.T,
	invocationStatus invocationstatuspb.InvocationStatus,
	success bool,
	failedTest string,
	chunks []invocationWatchTestChunk,
	chunkError error,
) *client {
	t.Helper()

	chunkByID := make(map[string]invocationWatchTestChunk, len(chunks))
	for _, chunk := range chunks {
		chunkByID[chunk.id] = chunk
	}
	firstChunkID := "chunk-1"
	if len(chunks) > 0 {
		firstChunkID = chunks[0].id
	}
	rpc := &invocationWatchTestRPC{
		getInvocation: func(context.Context, *invocationpb.GetInvocationRequest) (*invocationpb.GetInvocationResponse, error) {
			return invocationWatchTestResponse(invocationStatus, success, firstChunkID, true), nil
		},
		getEventLogChunk: func(_ context.Context, request *eventlogpb.GetEventLogChunkRequest) (*eventlogpb.GetEventLogChunkResponse, error) {
			if chunkError != nil {
				return nil, chunkError
			}
			chunk, ok := chunkByID[request.GetChunkId()]
			if !ok {
				t.Errorf("unexpected chunk ID %q", request.GetChunkId())
				return nil, grpcstatus.Error(codes.NotFound, "unexpected chunk ID")
			}
			return &eventlogpb.GetEventLogChunkResponse{Buffer: []byte(chunk.buffer), NextChunkId: chunk.nextChunkID}, nil
		},
		getTarget: func(context.Context, *targetpb.GetTargetRequest) (*targetpb.GetTargetResponse, error) {
			return invocationWatchTestTargetResponse(failedTest), nil
		},
	}
	return &client{baseURL: "https://app.buildbuddy.io", rpc: rpc}
}

func newInvocationWatchTestClient(
	t *testing.T,
	invocationStatus invocationstatuspb.InvocationStatus,
	success bool,
	failedTest string,
) (*client, func() (int, int, *targetpb.GetTargetRequest)) {
	t.Helper()

	invocationCalls := 0
	targetCalls := 0
	var targetRequest *targetpb.GetTargetRequest
	var mu sync.Mutex
	rpc := &invocationWatchTestRPC{
		getInvocation: func(context.Context, *invocationpb.GetInvocationRequest) (*invocationpb.GetInvocationResponse, error) {
			mu.Lock()
			invocationCalls++
			mu.Unlock()
			return invocationWatchTestResponse(invocationStatus, success, "", false), nil
		},
		getTarget: func(_ context.Context, request *targetpb.GetTargetRequest) (*targetpb.GetTargetResponse, error) {
			mu.Lock()
			targetCalls++
			targetRequest = request
			mu.Unlock()
			return invocationWatchTestTargetResponse(failedTest), nil
		},
	}
	return &client{baseURL: "https://app.buildbuddy.io", rpc: rpc}, func() (int, int, *targetpb.GetTargetRequest) {
		mu.Lock()
		defer mu.Unlock()
		return invocationCalls, targetCalls, targetRequest
	}
}

func readInvocationWatchResult(t *testing.T, path string) invocationWatchResult {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read result file: %v", err)
	}
	var result invocationWatchResult
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatalf("decode result file: %v\n%s", err, data)
	}
	return result
}

func TestWatchInvocationWritesEarlyTestFailureResult(t *testing.T) {
	const invocationID = "invocation-test"
	const failedTest = "//pkg:failed_test"
	client, requestCounts := newInvocationWatchTestClient(t, invocationstatuspb.InvocationStatus_PARTIAL_INVOCATION_STATUS, false, failedTest)
	resultPath := filepath.Join(t.TempDir(), "result.json")
	var logs bytes.Buffer

	err := watchInvocation(client, invocationID, &logs, time.Millisecond, true, resultPath)
	if err == nil || err.Error() != "first non-passing test result: //pkg:failed_test" {
		t.Fatalf("watchInvocation error = %v, want first failed target", err)
	}
	result := readInvocationWatchResult(t, resultPath)
	if result.Version != 1 || result.InvocationID != invocationID || result.URL != client.baseURL+"/invocation/"+invocationID {
		t.Errorf("result identity fields = %+v", result)
	}
	if result.State != "failure" || result.Reason != "test_failure" || result.InvocationComplete || result.FailedTest != failedTest {
		t.Errorf("early failure result = %+v", result)
	}
	invocations, targets, request := requestCounts()
	if invocations != 2 || targets != 1 {
		t.Errorf("API call counts = GetInvocation %d, GetTarget %d; want 2 and 1", invocations, targets)
	}
	if request.GetInvocationId() != invocationID || request.GetRequestContext().GetGroupId() != "test-group" || request.GetStatus().String() != "FAILED" {
		t.Errorf("GetTarget request = %+v", request)
	}
}

func TestWatchInvocationWritesTerminalResult(t *testing.T) {
	tests := []struct {
		name         string
		status       invocationstatuspb.InvocationStatus
		success      bool
		wantState    string
		wantReason   string
		wantComplete bool
		wantError    bool
	}{
		{
			name:         "success",
			status:       invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS,
			success:      true,
			wantState:    "success",
			wantReason:   "completed",
			wantComplete: true,
		},
		{
			name:         "failure",
			status:       invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS,
			wantState:    "failure",
			wantReason:   "completed",
			wantComplete: true,
		},
		{
			name:       "disconnected",
			status:     invocationstatuspb.InvocationStatus_DISCONNECTED_INVOCATION_STATUS,
			wantState:  "error",
			wantReason: "disconnected",
			wantError:  true,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client, _ := newInvocationWatchTestClient(t, test.status, test.success, "")
			resultPath := filepath.Join(t.TempDir(), "result.json")
			var logs bytes.Buffer

			err := watchInvocation(client, "invocation-test", &logs, time.Millisecond, false, resultPath)
			if (err != nil) != test.wantError {
				t.Fatalf("watchInvocation error = %v, wantError %v", err, test.wantError)
			}
			result := readInvocationWatchResult(t, resultPath)
			if result.State != test.wantState || result.Reason != test.wantReason || result.InvocationComplete != test.wantComplete {
				t.Errorf("terminal result = %+v", result)
			}
		})
	}
}

func TestWatchInvocationResultWhenProtocolLogReadFails(t *testing.T) {
	tests := []struct {
		name         string
		status       invocationstatuspb.InvocationStatus
		success      bool
		wantState    string
		wantReason   string
		wantComplete bool
	}{
		{
			name:         "success",
			status:       invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS,
			success:      true,
			wantState:    "success",
			wantReason:   "completed",
			wantComplete: true,
		},
		{
			name:         "failure",
			status:       invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS,
			wantState:    "failure",
			wantReason:   "completed",
			wantComplete: true,
		},
		{
			name:       "disconnected",
			status:     invocationstatuspb.InvocationStatus_DISCONNECTED_INVOCATION_STATUS,
			wantState:  "error",
			wantReason: "disconnected",
		},
		{
			name:       "still-running",
			status:     invocationstatuspb.InvocationStatus_PARTIAL_INVOCATION_STATUS,
			wantState:  "pending",
			wantReason: "watching",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client := newChunkedInvocationWatchTestClient(
				t,
				test.status,
				test.success,
				"",
				nil,
				grpcstatus.Error(codes.PermissionDenied, "event-log access denied"),
			)
			resultPath := filepath.Join(t.TempDir(), "result.json")
			var logs bytes.Buffer

			err := watchInvocation(client, "invocation-test", &logs, time.Millisecond, false, resultPath)
			if err == nil || grpcstatus.Code(err) != codes.PermissionDenied || !strings.Contains(err.Error(), "event-log access denied") {
				t.Fatalf("watchInvocation error = %v, want event-log permission failure", err)
			}
			result := readInvocationWatchResult(t, resultPath)
			if result.State != test.wantState || result.Reason != test.wantReason || result.InvocationComplete != test.wantComplete {
				t.Errorf("result after event-log read failure = %+v", result)
			}
		})
	}
}

func TestWatchInvocationPreservesInitialTerminalOutcomeWhenRefreshFails(t *testing.T) {
	invocationCalls := 0
	rpc := &invocationWatchTestRPC{
		getInvocation: func(context.Context, *invocationpb.GetInvocationRequest) (*invocationpb.GetInvocationResponse, error) {
			invocationCalls++
			if invocationCalls > 1 {
				return nil, grpcstatus.Error(codes.Unavailable, "BuildBuddy unavailable")
			}
			return invocationWatchTestResponse(invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS, true, "", false), nil
		},
	}
	client := &client{baseURL: "https://app.buildbuddy.io", rpc: rpc}
	resultPath := filepath.Join(t.TempDir(), "result.json")
	var logs bytes.Buffer

	err := watchInvocation(client, "invocation-test", &logs, time.Millisecond, false, resultPath)
	if err == nil || grpcstatus.Code(err) != codes.Unavailable || !strings.Contains(err.Error(), "BuildBuddy unavailable") {
		t.Fatalf("watchInvocation error = %v, want refresh failure", err)
	}
	result := readInvocationWatchResult(t, resultPath)
	if result.State != "success" || result.Reason != "completed" || !result.InvocationComplete {
		t.Errorf("result after refresh failure = %+v", result)
	}
}

func TestWatchInvocationStreamsTerminalChunks(t *testing.T) {
	client := newChunkedInvocationWatchTestClient(
		t,
		invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS,
		true,
		"",
		[]invocationWatchTestChunk{
			{id: "chunk-1", nextChunkID: "chunk-2", buffer: "first chunk\n"},
			{id: "chunk-2", buffer: "second chunk\n"},
		},
		nil,
	)
	resultPath := filepath.Join(t.TempDir(), "result.json")
	var logs bytes.Buffer

	if err := watchInvocation(client, "invocation-test", &logs, time.Millisecond, false, resultPath); err != nil {
		t.Fatalf("watchInvocation error = %v", err)
	}
	if !strings.HasSuffix(logs.String(), "first chunk\nsecond chunk\n") {
		t.Errorf("streamed logs = %q", logs.String())
	}
	result := readInvocationWatchResult(t, resultPath)
	if result.State != "success" || result.Reason != "completed" || !result.InvocationComplete {
		t.Errorf("terminal result = %+v", result)
	}
}

func TestWatchInvocationRecoversFromUnreadableTailAndStreamsHistoryAndLiveCursor(t *testing.T) {
	const invocationID = "invocation-test"
	invocationCalls := 0
	chunkIDs := make([]string, 0, 5)
	liveChunkCalls := 0
	rpc := &invocationWatchTestRPC{
		getInvocation: func(context.Context, *invocationpb.GetInvocationRequest) (*invocationpb.GetInvocationResponse, error) {
			invocationCalls++
			status := invocationstatuspb.InvocationStatus_PARTIAL_INVOCATION_STATUS
			success := false
			if invocationCalls >= 6 {
				status = invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS
				success = true
			}
			return invocationWatchTestResponse(status, success, "ffff", true), nil
		},
		getEventLogChunk: func(_ context.Context, request *eventlogpb.GetEventLogChunkRequest) (*eventlogpb.GetEventLogChunkResponse, error) {
			chunkID := request.GetChunkId()
			chunkIDs = append(chunkIDs, chunkID)
			switch chunkID {
			case "ffff":
				return nil, grpcstatus.Error(codes.ResourceExhausted, "Log index limit exceeded")
			case "0000":
				return &eventlogpb.GetEventLogChunkResponse{Buffer: []byte("history\n"), NextChunkId: "0001"}, nil
			case "0001":
				liveChunkCalls++
				if liveChunkCalls == 1 {
					return &eventlogpb.GetEventLogChunkResponse{Buffer: []byte("live"), Live: true}, nil
				}
				return &eventlogpb.GetEventLogChunkResponse{Buffer: []byte("live tail\n"), NextChunkId: "0002"}, nil
			case "0002":
				return &eventlogpb.GetEventLogChunkResponse{Buffer: []byte("done\n")}, nil
			default:
				t.Errorf("unexpected chunk ID %q", chunkID)
				return nil, grpcstatus.Error(codes.NotFound, "unexpected chunk ID")
			}
		},
		getTarget: func(context.Context, *targetpb.GetTargetRequest) (*targetpb.GetTargetResponse, error) {
			return &targetpb.GetTargetResponse{}, nil
		},
	}
	client := &client{baseURL: "https://app.buildbuddy.io", rpc: rpc}
	resultPath := filepath.Join(t.TempDir(), "result.json")
	var logs bytes.Buffer

	if err := watchInvocation(client, invocationID, &logs, time.Millisecond, false, resultPath); err != nil {
		t.Fatalf("watchInvocation error = %v", err)
	}
	if got, want := chunkIDs, []string{"ffff", "0000", "0001", "0001", "0002"}; !reflect.DeepEqual(got, want) {
		t.Errorf("requested chunk IDs = %v, want %v", got, want)
	}
	if got, want := logs.String(), "Watching BuildBuddy invocation invocation-test (https://app.buildbuddy.io/invocation/invocation-test)\nhistory\nlive tail\ndone\n"; got != want {
		t.Errorf("streamed logs = %q, want %q", got, want)
	}
	result := readInvocationWatchResult(t, resultPath)
	if result.State != "success" || result.Reason != "completed" || !result.InvocationComplete {
		t.Errorf("terminal result = %+v", result)
	}
}

func TestWatchInvocationContinuesStatusAfterPersistentLogIndexLimit(t *testing.T) {
	const invocationID = "invocation-test"
	invocationCalls := 0
	chunkIDs := make([]string, 0, 2)
	rpc := &invocationWatchTestRPC{
		getInvocation: func(context.Context, *invocationpb.GetInvocationRequest) (*invocationpb.GetInvocationResponse, error) {
			invocationCalls++
			status := invocationstatuspb.InvocationStatus_PARTIAL_INVOCATION_STATUS
			success := false
			if invocationCalls >= 4 {
				status = invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS
				success = true
			}
			return invocationWatchTestResponse(status, success, "ffff", true), nil
		},
		getEventLogChunk: func(_ context.Context, request *eventlogpb.GetEventLogChunkRequest) (*eventlogpb.GetEventLogChunkResponse, error) {
			chunkIDs = append(chunkIDs, request.GetChunkId())
			return nil, grpcstatus.Error(codes.ResourceExhausted, "Log index limit exceeded")
		},
		getTarget: func(context.Context, *targetpb.GetTargetRequest) (*targetpb.GetTargetResponse, error) {
			return &targetpb.GetTargetResponse{}, nil
		},
	}
	client := &client{baseURL: "https://app.buildbuddy.io", rpc: rpc}
	resultPath := filepath.Join(t.TempDir(), "result.json")
	var logs bytes.Buffer

	err := watchInvocation(client, invocationID, &logs, time.Millisecond, false, resultPath)
	if err == nil || grpcstatus.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), "Log index limit exceeded") {
		t.Fatalf("watchInvocation error = %v, want the deferred event-log failure", err)
	}
	if got, want := chunkIDs, []string{"ffff", "0000"}; !reflect.DeepEqual(got, want) {
		t.Errorf("requested chunk IDs = %v, want one fallback attempt: %v", got, want)
	}
	if invocationCalls != 4 {
		t.Errorf("GetInvocation calls = %d, want 4 including terminal observation", invocationCalls)
	}
	result := readInvocationWatchResult(t, resultPath)
	if result.State != "success" || result.Reason != "completed" || !result.InvocationComplete {
		t.Errorf("terminal result after log failure = %+v", result)
	}
}

func TestWatchInvocationFailFastTargetOverridesObservedCompletion(t *testing.T) {
	const failedTest = "//pkg:failed_test"
	client := newChunkedInvocationWatchTestClient(
		t,
		invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS,
		false,
		failedTest,
		[]invocationWatchTestChunk{{id: "chunk-1", buffer: "first chunk\n"}},
		nil,
	)
	resultPath := filepath.Join(t.TempDir(), "result.json")
	var logs bytes.Buffer

	err := watchInvocation(client, "invocation-test", &logs, time.Millisecond, true, resultPath)
	if err == nil || !strings.Contains(err.Error(), failedTest) {
		t.Fatalf("watchInvocation error = %v, want failing target", err)
	}
	result := readInvocationWatchResult(t, resultPath)
	if result.State != "failure" || result.Reason != "test_failure" || !result.InvocationComplete || result.FailedTest != failedTest {
		t.Errorf("fail-fast result = %+v", result)
	}
}

func TestWatchInvocationLeavesPendingResultWhenLookupFails(t *testing.T) {
	rpc := &invocationWatchTestRPC{
		getInvocation: func(context.Context, *invocationpb.GetInvocationRequest) (*invocationpb.GetInvocationResponse, error) {
			return nil, grpcstatus.Error(codes.Unavailable, "BuildBuddy unavailable")
		},
	}
	client := &client{baseURL: "https://app.buildbuddy.io", rpc: rpc}
	resultPath := filepath.Join(t.TempDir(), "result.json")
	var logs bytes.Buffer

	err := watchInvocation(client, "invocation-test", &logs, time.Millisecond, true, resultPath)
	if err == nil {
		t.Fatal("watchInvocation succeeded after GetInvocation failed")
	}
	result := readInvocationWatchResult(t, resultPath)
	if result.State != "pending" || result.Reason != "watching" || result.InvocationComplete {
		t.Errorf("result after failed lookup = %+v", result)
	}
}

package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type targetRequestJSON struct {
	RequestContext struct {
		GroupID string `json:"groupId"`
	} `json:"requestContext"`
	InvocationID string `json:"invocationId"`
	Status       string `json:"status"`
}

type eventLogChunkRequestJSON struct {
	ChunkID string `json:"chunkId"`
}

type invocationWatchTestChunk struct {
	id          string
	nextChunkID string
	buffer      string
}

func newChunkedInvocationWatchTestClient(
	t *testing.T,
	invocationStatus string,
	success bool,
	failedTest string,
	chunks []invocationWatchTestChunk,
	chunkErrorStatus int,
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
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("request method = %q, want POST", r.Method)
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/rpc/BuildBuddyService/GetInvocation":
			_, _ = fmt.Fprintf(
				w,
				`{"invocation":[{"acl":{"groupId":"test-group"},"invocationStatus":%q,"success":%t,"hasChunkedEventLogs":true,"lastChunkId":%q}]}`,
				invocationStatus,
				success,
				firstChunkID,
			)
		case "/rpc/BuildBuddyService/GetEventLogChunk":
			if chunkErrorStatus != 0 {
				http.Error(w, "Log index limit exceeded", chunkErrorStatus)
				return
			}
			var request eventLogChunkRequestJSON
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Errorf("decode GetEventLogChunk request: %v", err)
				http.Error(w, "bad request", http.StatusBadRequest)
				return
			}
			chunk, ok := chunkByID[request.ChunkID]
			if !ok {
				t.Errorf("unexpected chunk ID %q", request.ChunkID)
				http.NotFound(w, r)
				return
			}
			buffer := base64.StdEncoding.EncodeToString([]byte(chunk.buffer))
			_, _ = fmt.Fprintf(w, `{"buffer":%q,"nextChunkId":%q}`, buffer, chunk.nextChunkID)
		case "/rpc/BuildBuddyService/GetTarget":
			if failedTest == "" {
				_, _ = io.WriteString(w, `{}`)
				return
			}
			_, _ = fmt.Fprintf(w, `{"targetGroups":[{"targets":[{"metadata":{"label":%q}}]}]}`, failedTest)
		default:
			t.Errorf("unexpected API path %q", r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)

	return &client{baseURL: server.URL, apiKey: "test-key", http: server.Client()}
}

func newInvocationWatchTestClient(
	t *testing.T,
	invocationStatus string,
	success bool,
	failedTest string,
) (*client, func() (int, int, targetRequestJSON)) {
	t.Helper()

	invocationCalls := 0
	targetCalls := 0
	var targetRequest targetRequestJSON
	var mu sync.Mutex
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("request method = %q, want POST", r.Method)
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/rpc/BuildBuddyService/GetInvocation":
			mu.Lock()
			invocationCalls++
			mu.Unlock()
			_, _ = fmt.Fprintf(
				w,
				`{"invocation":[{"acl":{"groupId":"test-group"},"invocationStatus":%q,"success":%t,"hasChunkedEventLogs":false}]}`,
				invocationStatus,
				success,
			)
		case "/rpc/BuildBuddyService/GetTarget":
			mu.Lock()
			targetCalls++
			mu.Unlock()
			var request targetRequestJSON
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Errorf("decode GetTarget request: %v", err)
				http.Error(w, "bad request", http.StatusBadRequest)
				return
			}
			mu.Lock()
			targetRequest = request
			mu.Unlock()
			if failedTest == "" {
				_, _ = io.WriteString(w, `{}`)
				return
			}
			_, _ = fmt.Fprintf(w, `{"targetGroups":[{"targets":[{"metadata":{"label":%q}}]}]}`, failedTest)
		default:
			t.Errorf("unexpected API path %q", r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)

	return &client{baseURL: server.URL, apiKey: "test-key", http: server.Client()}, func() (int, int, targetRequestJSON) {
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
	client, requestCounts := newInvocationWatchTestClient(t, "PARTIAL_INVOCATION_STATUS", false, failedTest)
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
	if request.InvocationID != invocationID || request.RequestContext.GroupID != "test-group" || request.Status != "FAILED" {
		t.Errorf("GetTarget request = %+v", request)
	}
}

func TestWatchInvocationWritesTerminalResult(t *testing.T) {
	tests := []struct {
		name         string
		status       string
		success      bool
		wantState    string
		wantReason   string
		wantComplete bool
		wantError    bool
	}{
		{
			name:         "success",
			status:       "COMPLETE_INVOCATION_STATUS",
			success:      true,
			wantState:    "success",
			wantReason:   "completed",
			wantComplete: true,
		},
		{
			name:         "failure",
			status:       "COMPLETE_INVOCATION_STATUS",
			wantState:    "failure",
			wantReason:   "completed",
			wantComplete: true,
		},
		{
			name:       "disconnected",
			status:     "DISCONNECTED_INVOCATION_STATUS",
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

func TestWatchInvocationResultWhenLogReadFails(t *testing.T) {
	tests := []struct {
		name         string
		status       string
		success      bool
		wantState    string
		wantReason   string
		wantComplete bool
	}{
		{
			name:         "success",
			status:       "COMPLETE_INVOCATION_STATUS",
			success:      true,
			wantState:    "success",
			wantReason:   "completed",
			wantComplete: true,
		},
		{
			name:         "failure",
			status:       "COMPLETE_INVOCATION_STATUS",
			wantState:    "failure",
			wantReason:   "completed",
			wantComplete: true,
		},
		{
			name:       "disconnected",
			status:     "DISCONNECTED_INVOCATION_STATUS",
			wantState:  "error",
			wantReason: "disconnected",
		},
		{
			name:       "still-running",
			status:     "PARTIAL_INVOCATION_STATUS",
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
				http.StatusTooManyRequests,
			)
			resultPath := filepath.Join(t.TempDir(), "result.json")
			var logs bytes.Buffer

			err := watchInvocation(client, "invocation-test", &logs, time.Millisecond, false, resultPath)
			if err == nil || !strings.Contains(err.Error(), "read BuildBuddy event log: HTTP 429") {
				t.Fatalf("watchInvocation error = %v, want event-log read failure", err)
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
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/rpc/BuildBuddyService/GetInvocation" {
			t.Errorf("unexpected API path %q", r.URL.Path)
			http.NotFound(w, r)
			return
		}
		invocationCalls++
		if invocationCalls > 1 {
			http.Error(w, "BuildBuddy unavailable", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"invocation":[{"acl":{"groupId":"test-group"},"invocationStatus":"COMPLETE_INVOCATION_STATUS","success":true,"hasChunkedEventLogs":false}]}`)
	}))
	t.Cleanup(server.Close)
	client := &client{baseURL: server.URL, apiKey: "test-key", http: server.Client()}
	resultPath := filepath.Join(t.TempDir(), "result.json")
	var logs bytes.Buffer

	err := watchInvocation(client, "invocation-test", &logs, time.Millisecond, false, resultPath)
	if err == nil || !strings.Contains(err.Error(), "HTTP 503") {
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
		"COMPLETE_INVOCATION_STATUS",
		true,
		"",
		[]invocationWatchTestChunk{
			{id: "chunk-1", nextChunkID: "chunk-2", buffer: "first chunk\n"},
			{id: "chunk-2", buffer: "second chunk\n"},
		},
		0,
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

func TestWatchInvocationFailFastTargetOverridesObservedCompletion(t *testing.T) {
	const failedTest = "//pkg:failed_test"
	client := newChunkedInvocationWatchTestClient(
		t,
		"COMPLETE_INVOCATION_STATUS",
		false,
		failedTest,
		[]invocationWatchTestChunk{{id: "chunk-1", buffer: "first chunk\n"}},
		0,
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
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "BuildBuddy unavailable", http.StatusServiceUnavailable)
	}))
	t.Cleanup(server.Close)
	client := &client{baseURL: server.URL, apiKey: "test-key", http: server.Client()}
	resultPath := filepath.Join(t.TempDir(), "result.json")
	var logs bytes.Buffer

	err := watchInvocation(client, "invocation-test", &logs, time.Millisecond, true, resultPath)
	if err == nil {
		t.Fatal("watchInvocation succeeded after BuildBuddy returned 503")
	}
	result := readInvocationWatchResult(t, resultPath)
	if result.State != "pending" || result.Reason != "watching" || result.InvocationComplete {
		t.Errorf("result after failed lookup = %+v", result)
	}
}

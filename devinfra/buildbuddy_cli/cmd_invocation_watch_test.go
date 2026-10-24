package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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

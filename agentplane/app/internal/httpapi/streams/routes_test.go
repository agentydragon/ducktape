package streams

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
)

func TestWriteSSEPreservesJSONData(t *testing.T) {
	response := httptest.NewRecorder()
	startSSE(response)
	if err := writeSSE(response, "event", map[string]any{"kind": "tool", "text": "hello"}); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || response.Header().Get("Content-Type") != "text/event-stream; charset=utf-8" || response.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("SSE response = %d %#v", response.Code, response.Header())
	}
	for _, want := range []string{"event: event\n", `data: {"kind":"tool","text":"hello"}`, "\n\n"} {
		if !strings.Contains(response.Body.String(), want) {
			t.Fatalf("SSE frame %q does not contain %q", response.Body.String(), want)
		}
	}
}

func TestConsoleRelayStreamsBase64OutputAndClosesOnCancellation(t *testing.T) {
	reader, writer := io.Pipe()
	response := newStreamingRecorder()
	startSSE(response)
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
		cancel()
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

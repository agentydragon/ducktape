package facade

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// dispatcher records what its servicer received over gRPC and answers from canned Dispatches.
type dispatcher struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	mu            sync.Mutex
	authorization []string
	filter        *dispatcherpb.DispatchFilter
	submitted     *dispatcherpb.SubmitInputRequest
	cancelled     string
	unaryStarted  chan struct{}
	unaryCanceled chan struct{}
	watchStarted  chan struct{}
	watchCanceled chan struct{}
}

func (d *dispatcher) GetDispatch(ctx context.Context, request *dispatcherpb.GetDispatchRequest) (*dispatcherpb.GetDispatchResponse, error) {
	d.mu.Lock()
	d.authorization = metadata.ValueFromIncomingContext(ctx, "authorization")
	d.mu.Unlock()
	if d.unaryStarted != nil {
		close(d.unaryStarted)
		<-ctx.Done()
		close(d.unaryCanceled)
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	if request.GetCommandId() == "missing" {
		return nil, status.Error(codes.NotFound, "dispatch not found")
	}
	return &dispatcherpb.GetDispatchResponse{Dispatch: &dispatcherpb.Dispatch{
		CommandId: request.GetCommandId(), State: dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED,
	}}, nil
}

func (d *dispatcher) SubmitInput(_ context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	d.mu.Lock()
	d.submitted = request
	d.mu.Unlock()
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{
		CommandId: request.GetCommandId(), State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
	}}, nil
}

func (d *dispatcher) CancelDispatch(_ context.Context, request *dispatcherpb.CancelDispatchRequest) (*dispatcherpb.CancelDispatchResponse, error) {
	d.mu.Lock()
	d.cancelled = request.GetCommandId()
	d.mu.Unlock()
	return &dispatcherpb.CancelDispatchResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (d *dispatcher) WatchDispatches(request *dispatcherpb.WatchDispatchesRequest, stream grpc.ServerStreamingServer[dispatcherpb.WatchDispatchesResponse]) error {
	d.mu.Lock()
	d.filter = request.GetFilter()
	d.mu.Unlock()
	if d.watchStarted != nil {
		close(d.watchStarted)
		<-stream.Context().Done()
		close(d.watchCanceled)
		return status.FromContextError(stream.Context().Err()).Err()
	}
	for _, frame := range []*dispatcherpb.WatchDispatchesResponse{
		{Frame: &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: &dispatcherpb.Dispatch{CommandId: "command-a"}}},
		{Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}}},
	} {
		if err := stream.Send(frame); err != nil {
			return err
		}
	}
	return nil
}

func serve(t *testing.T) (*dispatcher, string) {
	t.Helper()
	return serveWithUnaryTimeout(t, 0, &dispatcher{})
}

func serveWithUnaryTimeout(t *testing.T, unaryTimeout time.Duration, servicer *dispatcher) (*dispatcher, string) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	dispatcherpb.RegisterSessionDispatcherServer(server, servicer)
	go server.Serve(listener)
	t.Cleanup(server.Stop)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	handler, err := NewHandler(ctx, listener.Addr().String(), unaryTimeout, dispatcherpb.RegisterSessionDispatcherHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	facade := httptest.NewServer(handler)
	t.Cleanup(facade.Close)
	return servicer, facade.URL
}

func TestUnaryTimeoutCancelsRPC(t *testing.T) {
	servicer := &dispatcher{unaryStarted: make(chan struct{}), unaryCanceled: make(chan struct{})}
	_, facade := serveWithUnaryTimeout(t, 50*time.Millisecond, servicer)
	response := get(t, facade+"/v1/dispatches/command-1", "Bearer token-1")
	if response.StatusCode != http.StatusGatewayTimeout {
		t.Fatalf("status = %d, want %d", response.StatusCode, http.StatusGatewayTimeout)
	}
	select {
	case <-servicer.unaryCanceled:
	case <-time.After(time.Second):
		t.Fatal("configured unary timeout did not cancel the gRPC call")
	}
}

func TestUnaryTimeoutDoesNotBoundStreamAndRequestCancellationStillDoes(t *testing.T) {
	const unaryTimeout = 50 * time.Millisecond
	servicer := &dispatcher{watchStarted: make(chan struct{}), watchCanceled: make(chan struct{})}
	_, facade := serveWithUnaryTimeout(t, unaryTimeout, servicer)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, facade+"/v1/dispatches:watch", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer token-1")
	type result struct {
		response *http.Response
		err      error
	}
	completed := make(chan result, 1)
	go func() {
		response, requestErr := http.DefaultClient.Do(request)
		completed <- result{response: response, err: requestErr}
	}()
	select {
	case <-servicer.watchStarted:
	case <-time.After(time.Second):
		t.Fatal("server-streaming RPC did not start")
	}
	select {
	case <-servicer.watchCanceled:
		t.Fatal("unary timeout incorrectly bounded a streaming RPC")
	case <-time.After(3 * unaryTimeout):
	}
	cancel()
	select {
	case <-servicer.watchCanceled:
	case <-time.After(time.Second):
		t.Fatal("HTTP request cancellation did not cancel the streaming RPC")
	}
	select {
	case outcome := <-completed:
		if outcome.response != nil {
			_ = outcome.response.Body.Close()
		}
	case <-time.After(time.Second):
		t.Fatal("HTTP request did not finish after cancellation")
	}
}

func get(t *testing.T, url, authorization string) *http.Response {
	t.Helper()
	return send(t, http.MethodGet, url, authorization, "")
}

func send(t *testing.T, method, url, authorization, body string) *http.Response {
	t.Helper()
	request, err := http.NewRequest(method, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if authorization != "" {
		request.Header.Set("Authorization", authorization)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { response.Body.Close() })
	return response
}

func TestUnaryCallForwardsBearerAndAnswersJSON(t *testing.T) {
	servicer, facade := serve(t)
	response := get(t, facade+"/v1/dispatches/command-1", "Bearer token-1")
	var body struct {
		Dispatch struct {
			CommandID string `json:"commandId"`
			State     string `json:"state"`
		} `json:"dispatch"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || body.Dispatch.CommandID != "command-1" || body.Dispatch.State != "DELIVERY_STATE_RUNNER_ADMITTED" {
		t.Fatalf("status %d, body %+v", response.StatusCode, body)
	}
	servicer.mu.Lock()
	defer servicer.mu.Unlock()
	if !slices.Equal(servicer.authorization, []string{"Bearer token-1"}) {
		t.Fatalf("servicer saw authorization %q", servicer.authorization)
	}
}

func TestGRPCStatusBecomesHTTPStatus(t *testing.T) {
	_, facade := serve(t)
	response := get(t, facade+"/v1/dispatches/missing", "Bearer token-1")
	var body struct {
		Code int `json:"code"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusNotFound || body.Code != int(codes.NotFound) {
		t.Fatalf("status %d, body %+v", response.StatusCode, body)
	}
}

func TestServerStreamAnswersOneResultObjectPerLine(t *testing.T) {
	servicer, facade := serve(t)
	response := get(t, facade+"/v1/dispatches:watch?filter.sessionId=session-1&filter.commandIds=command-a&filter.commandIds=command-b", "Bearer token-1")
	var frames []map[string]map[string]any
	lines := bufio.NewScanner(response.Body)
	for lines.Scan() {
		var frame map[string]map[string]any
		if err := json.Unmarshal(lines.Bytes(), &frame); err != nil {
			t.Fatalf("line %q: %v", lines.Bytes(), err)
		}
		frames = append(frames, frame)
	}
	if err := lines.Err(); err != nil {
		t.Fatal(err)
	}
	if len(frames) != 2 {
		t.Fatalf("frames %v", frames)
	}
	dispatch, _ := frames[0]["result"]["dispatch"].(map[string]any)
	if dispatch["commandId"] != "command-a" {
		t.Fatalf("first frame %v", frames[0])
	}
	if _, caughtUp := frames[1]["result"]["caughtUp"]; !caughtUp {
		t.Fatalf("second frame %v", frames[1])
	}
	servicer.mu.Lock()
	defer servicer.mu.Unlock()
	if servicer.filter.GetSessionId() != "session-1" || !slices.Equal(servicer.filter.GetCommandIds(), []string{"command-a", "command-b"}) {
		t.Fatalf("servicer saw filter %v", servicer.filter)
	}
}

func TestCommandTakesSessionFromPathAndRestFromBody(t *testing.T) {
	servicer, facade := serve(t)
	response := send(t, http.MethodPost, facade+"/v1/sessions/session-1:submitInput", "Bearer token-1",
		`{"commandId": "command-1", "sandbox": "sandbox-1", "text": "hello"}`)
	var body struct {
		Dispatch struct{ State string } `json:"dispatch"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || body.Dispatch.State != "DELIVERY_STATE_QUEUED_IN_DISPATCHER" {
		t.Fatalf("status %d, body %+v", response.StatusCode, body)
	}
	servicer.mu.Lock()
	defer servicer.mu.Unlock()
	if got := servicer.submitted; got.GetSessionId() != "session-1" || got.GetCommandId() != "command-1" || got.GetSandbox() != "sandbox-1" || got.GetText() != "hello" {
		t.Fatalf("servicer saw %v", got)
	}
}

func TestCommandWithOnlyAPathParameterTakesAnEmptyBody(t *testing.T) {
	servicer, facade := serve(t)
	response := send(t, http.MethodPost, facade+"/v1/dispatches/command-2:cancel", "Bearer token-1", "{}")
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status %d", response.StatusCode)
	}
	servicer.mu.Lock()
	defer servicer.mu.Unlock()
	if servicer.cancelled != "command-2" {
		t.Fatalf("servicer cancelled %q", servicer.cancelled)
	}
}

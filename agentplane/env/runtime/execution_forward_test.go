package runtime

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/coder/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type directExecutionTestClient struct {
	ctx         context.Context
	cancel      context.CancelFunc
	conn        *grpc.ClientConn
	runner      environmentruntimepb.EnvironmentRuntimeClient
	executor    executorpb.ExecutorClient
	callCtx     context.Context
	bootID      string
	incarnation string
	identity    configuration.SandboxIdentity
}

func startDirectExecutionTestClient(t *testing.T, r *Runtime, incarnation string) *directExecutionTestClient {
	t.Helper()
	listener, server, err := r.control.Listen(r.executorProxy)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	credentials, err := r.config.Control.Security.ClientCredentials("")
	if err != nil {
		cancel()
		server.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(credentials))
	if err != nil {
		cancel()
		server.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	runnerClient := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	watch, err := runnerClient.WatchState(ctx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: r.config.Launch.Sandbox.UID, IncarnationUid: incarnation, ConnectionEpoch: 1,
	})
	if err != nil {
		cancel()
		_ = conn.Close()
		server.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		cancel()
		_ = conn.Close()
		server.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	callCtx := metadata.AppendToOutgoingContext(
		ctx,
		control.SandboxMetadata, r.config.Launch.Sandbox.UID,
		control.IncarnationMetadata, incarnation,
		control.BootMetadata, first.GetEnvironmentRuntimeBootId(),
		control.EpochMetadata, "1",
	)
	t.Cleanup(func() {
		cancel()
		server.Stop()
		_ = listener.Close()
		_ = conn.Close()
	})
	return &directExecutionTestClient{
		ctx: ctx, cancel: cancel, conn: conn, runner: runnerClient,
		executor: executorpb.NewExecutorClient(conn), callCtx: callCtx,
		bootID: first.GetEnvironmentRuntimeBootId(), incarnation: incarnation, identity: r.config.Launch.Sandbox,
	}
}

func (h *directExecutionTestClient) open(ctx context.Context, sessionID, requestID string) (executorpb.Executor_OpenSessionClient, error) {
	return h.executor.OpenSession(ctx, &executorpb.OpenSessionRequest{Scope: h.scope(sessionID), RequestId: requestID})
}

func (h *directExecutionTestClient) scope(sessionID string) *executorpb.ExecutionScope {
	return &executorpb.ExecutionScope{
		Destination: &sandboxespb.SandboxDestination{
			Sandbox: h.identity.Name, SandboxUid: h.identity.UID,
			Owner: &sandboxespb.ServiceAccount{Namespace: h.identity.Namespace, Name: h.identity.Name},
		},
		ExpectedIncarnationUid: h.incarnation,
		SessionId:              sessionID,
	}
}

func (h *directExecutionTestClient) replaceWatch(incarnation string, epoch uint64) error {
	watch, err := h.runner.WatchState(h.ctx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: h.identity.UID, IncarnationUid: incarnation, ConnectionEpoch: epoch,
	})
	if err != nil {
		return err
	}
	first, err := watch.Recv()
	if err != nil {
		return err
	}
	h.incarnation = incarnation
	h.bootID = first.GetEnvironmentRuntimeBootId()
	h.callCtx = metadata.AppendToOutgoingContext(
		h.ctx,
		control.SandboxMetadata, h.identity.UID,
		control.IncarnationMetadata, incarnation,
		control.BootMetadata, h.bootID,
		control.EpochMetadata, uint64String(epoch),
	)
	return nil
}

func startNativeExecutionTestServer(t *testing.T, notify bool) (string, <-chan string, <-chan string) {
	t.Helper()
	opened := make(chan string, 8)
	closed := make(chan string, 8)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(w, request, nil)
		if err != nil {
			return
		}
		var sessionID string
		defer func() {
			connection.CloseNow()
			if sessionID != "" {
				select {
				case closed <- sessionID:
				default:
				}
			}
		}()
		_, payload, err := connection.Read(request.Context())
		if err != nil {
			return
		}
		var initialize struct {
			ID string `json:"id"`
		}
		if json.Unmarshal(payload, &initialize) != nil || initialize.ID == "" {
			return
		}
		sessionID = "native-" + initialize.ID
		response := []byte(fmt.Sprintf(`{"id":%q,"result":{"sessionId":%q,"environmentInfo":{"shell":{"name":"bash","path":"/bin/bash"}}}}`, initialize.ID, sessionID))
		if connection.Write(request.Context(), websocket.MessageText, response) != nil {
			return
		}
		if _, _, err := connection.Read(request.Context()); err != nil { // initialized notification
			return
		}
		opened <- sessionID
		if notify {
			payload := []byte(fmt.Sprintf(`{"method":"codex/event","params":{"sessionId":%q}}`, sessionID))
			if connection.Write(request.Context(), websocket.MessageText, payload) != nil {
				return
			}
		}
		for {
			if _, _, err := connection.Read(request.Context()); err != nil {
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http"), opened, closed
}

func TestOpenSessionEndsWhenObserverEpochIsSuperseded(t *testing.T) {
	url, opened, closed := startNativeExecutionTestServer(t, false)
	r := newExecutionRuntime(t, url, nil)
	if _, err := os.Stat(r.config.ExecutionSocket); !os.IsNotExist(err) {
		t.Fatalf("direct Execution fixture unexpectedly served a private RPC socket: %v", err)
	}
	client := startDirectExecutionTestClient(t, r, "incarnation-1")
	stream, err := client.open(client.callCtx, "sampler-session", "open-before-supersession")
	if err != nil {
		t.Fatalf("OpenSession RPC: %v", err)
	}
	response, err := stream.Recv()
	if err != nil || response.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("OpenSession before supersession = %v, err=%v", response, err)
	}
	select {
	case <-opened:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("local execution session did not initialize")
	}
	second, err := client.open(client.callCtx, "second-session", "second-open-before-supersession")
	if err != nil {
		t.Fatalf("second OpenSession RPC: %v", err)
	}
	secondResponse, err := second.Recv()
	if err != nil || secondResponse.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("second OpenSession before supersession = %v, err=%v", secondResponse, err)
	}
	select {
	case <-opened:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("second local execution session did not initialize")
	}

	if err := client.replaceWatch("incarnation-2", 2); err != nil {
		t.Fatalf("replace observer epoch: %v", err)
	}
	wantClosed := map[string]bool{response.GetExecutionSessionId(): true, secondResponse.GetExecutionSessionId(): true}
	for range 2 {
		select {
		case closedID := <-closed:
			if !wantClosed[closedID] {
				t.Fatalf("observer supersession closed unexpected native session %q", closedID)
			}
			delete(wantClosed, closedID)
		case <-time.After(lifecycleTestTimeout):
			t.Fatalf("observer epoch supersession left native WebSockets open: %v", wantClosed)
		}
	}
	if _, err := stream.Recv(); err == nil {
		t.Fatal("OpenSession remained open after observer supersession")
	}
	if _, err := second.Recv(); err == nil {
		t.Fatal("second OpenSession remained open after observer supersession")
	}
	waitRuntime(t, r, func() bool { facts, err := r.facts(); return err == nil && facts.IdleSince != nil }, "superseded execution slot release")
}

type blockingDirectExecutor struct {
	executorpb.UnimplementedExecutorServer
	started chan struct{}
	release chan struct{}
}

func (e *blockingDirectExecutor) StartProcess(ctx context.Context, _ *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
	close(e.started)
	select {
	case <-e.release:
		return &executorpb.StartProcessResponse{}, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func TestDirectUnaryExecutionCallHoldsAdmissionUntilCompletion(t *testing.T) {
	url, _, _ := startNativeExecutionTestServer(t, false)
	r := newExecutionRuntime(t, url, nil)
	client := startDirectExecutionTestClient(t, r, "incarnation-1")
	executor := &blockingDirectExecutor{started: make(chan struct{}), release: make(chan struct{})}
	r.mu.Lock()
	r.executionServer = executor
	r.mu.Unlock()
	completed := make(chan error, 1)
	go func() {
		_, err := client.executor.StartProcess(client.callCtx, &executorpb.StartProcessRequest{Scope: client.scope("unary-session")})
		completed <- err
	}()
	select {
	case <-executor.started:
	case err := <-completed:
		t.Fatalf("public StartProcess returned before direct dispatch: %v", err)
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("public StartProcess did not reach the direct executor")
	}
	r.mu.Lock()
	admissions, calls := r.activeAdmissions, r.activeExecutionCalls
	r.mu.Unlock()
	if admissions != 1 || calls != 1 {
		t.Fatalf("active admission/call = %d/%d, want 1/1 while direct call is active", admissions, calls)
	}
	close(executor.release)
	select {
	case err := <-completed:
		if err != nil {
			t.Fatalf("direct StartProcess: %v", err)
		}
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("direct StartProcess did not finish")
	}
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.activeAdmissions == 0 && r.activeExecutionCalls == 0
	}, "direct unary admission release")
}

func TestStaleExecutionIncarnationReportsVerifiedIdentity(t *testing.T) {
	url, _, _ := startNativeExecutionTestServer(t, false)
	r := newExecutionRuntime(t, url, nil)
	client := startDirectExecutionTestClient(t, r, "current-incarnation")
	open := func(scope *executorpb.ExecutionScope, requestID string) error {
		stream, err := client.executor.OpenSession(client.callCtx, &executorpb.OpenSessionRequest{Scope: scope, RequestId: requestID})
		if err != nil {
			return err
		}
		_, err = stream.Recv()
		return err
	}

	stale := client.scope("sampler-session")
	stale.ExpectedIncarnationUid = "previous-incarnation"
	err := open(stale, "stale-incarnation")
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale OpenSession = %v, want FailedPrecondition", err)
	}
	var changed *executorpb.SandboxIncarnationChanged
	for _, detail := range status.Convert(err).Details() {
		if candidate, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
			changed = candidate
		}
	}
	if changed == nil || changed.GetSandboxUid() != client.identity.UID || changed.GetIncarnationUid() != "current-incarnation" {
		t.Fatalf("stale OpenSession details = %v, want verified Sandbox %q and incarnation %q", status.Convert(err).Details(), client.identity.UID, "current-incarnation")
	}

	for _, test := range []struct {
		name   string
		mutate func(*executorpb.ExecutionScope)
		code   codes.Code
	}{
		{name: "incomplete scope", mutate: func(scope *executorpb.ExecutionScope) { scope.ExpectedIncarnationUid = "" }, code: codes.InvalidArgument},
		{name: "foreign Sandbox", mutate: func(scope *executorpb.ExecutionScope) { scope.Destination.SandboxUid = "another-sandbox" }, code: codes.PermissionDenied},
	} {
		t.Run(test.name, func(t *testing.T) {
			scope := client.scope("sampler-session")
			test.mutate(scope)
			err := open(scope, "rejected-scope")
			if status.Code(err) != test.code {
				t.Fatalf("OpenSession = %v, want %s", err, test.code)
			}
			for _, detail := range status.Convert(err).Details() {
				if _, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
					t.Fatalf("rejected request carried a rebind detail: %v", detail)
				}
			}
		})
	}
}

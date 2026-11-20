package coordinator

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/coder/websocket"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type directExecutionTestClient struct {
	ctx         context.Context
	cancel      context.CancelFunc
	conn        *grpc.ClientConn
	runtime     environmentruntimepb.EnvironmentRuntimeClient
	harnessHost harnesshostpb.HarnessHostClient
	executor    executorpb.ExecutorClient
	callCtx     context.Context
	stopWatch   func() error
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
	credentials, err := r.security.ClientCredentials("")
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
	runtimeClient := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	bindTestIncarnation(t, runtimeClient, r.config.Launch.Sandbox.UID, incarnation)
	watchCtx, stopWatch := context.WithCancel(ctx)
	watch, err := runtimeClient.WatchState(watchCtx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: r.config.Launch.Sandbox.UID, IncarnationUid: incarnation,
	})
	if err != nil {
		stopWatch()
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
	)
	t.Cleanup(func() {
		cancel()
		server.Stop()
		_ = listener.Close()
		_ = conn.Close()
	})
	return &directExecutionTestClient{
		ctx: ctx, cancel: cancel, conn: conn, runtime: runtimeClient, harnessHost: harnesshostpb.NewHarnessHostClient(conn),
		executor: executorpb.NewExecutorClient(conn), callCtx: callCtx, stopWatch: func() error {
			stopWatch()
			for {
				if _, err := watch.Recv(); err != nil {
					return err
				}
			}
		},
		bootID: first.GetEnvironmentRuntimeBootId(), incarnation: incarnation, identity: r.config.Launch.Sandbox,
	}
}

func (h *directExecutionTestClient) open(ctx context.Context, sessionID, requestID string) (executorpb.Executor_OpenSessionClient, error) {
	return h.executor.OpenSession(ctx, &executorpb.OpenSessionRequest{Scope: h.scope(sessionID), RequestId: requestID})
}

func (h *directExecutionTestClient) scope(sessionID string) *executorpb.ExecutionScope {
	return &executorpb.ExecutionScope{
		Destination: &identitypb.SandboxDestination{
			Target: &identitypb.EnvironmentTarget{Namespace: h.identity.Namespace, Name: h.identity.Name, Uid: h.identity.UID}, Owner: &identitypb.ServiceAccount{Namespace: h.identity.Namespace, Name: h.identity.Name},
		},
		ExpectedIncarnationUid: h.incarnation,
		SessionId:              sessionID,
	}
}

func (h *directExecutionTestClient) replaceWatch(incarnation string) error {
	if h.stopWatch != nil {
		_ = h.stopWatch()
	}
	watchCtx, stopWatch := context.WithCancel(h.ctx)
	watch, err := h.runtime.WatchState(watchCtx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: h.identity.UID, IncarnationUid: incarnation,
	})
	if err != nil {
		stopWatch()
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
	)
	h.stopWatch = func() error {
		stopWatch()
		for {
			if _, err := watch.Recv(); err != nil {
				return err
			}
		}
	}
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

func TestOpenSessionSurvivesGatewayObserverLoss(t *testing.T) {
	url, opened, _ := startNativeExecutionTestServer(t, false)
	r := newExecutionRuntime(t, url, nil)
	client := startDirectExecutionTestClient(t, r, "incarnation-1")
	stream, err := client.open(client.callCtx, "sampler-session", "open-before-supersession")
	if err != nil {
		t.Fatalf("OpenSession RPC: %v", err)
	}
	response, err := stream.Recv()
	if err != nil || response.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("OpenSession before supersession = %v, err=%v", response, err)
	}
	waitSignal(t, opened, "local execution session did not initialize")
	second, err := client.open(client.callCtx, "second-session", "second-open-before-supersession")
	if err != nil {
		t.Fatalf("second OpenSession RPC: %v", err)
	}
	secondResponse, err := second.Recv()
	if err != nil || secondResponse.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("second OpenSession before supersession = %v, err=%v", secondResponse, err)
	}
	waitSignal(t, opened, "second local execution session did not initialize")

	if err := client.stopWatch(); status.Code(err) != codes.Canceled {
		t.Fatalf("Gateway observer cancellation = %v, want Canceled", err)
	}
	if _, err := client.runtime.GetStatus(client.callCtx, &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("GetStatus after Gateway observer loss: %v", err)
	}
	r.mu.Lock()
	activeCalls := r.activeExecutionCalls
	r.mu.Unlock()
	if activeCalls != 2 {
		t.Fatalf("active execution calls after observer loss = %d, want both native sessions retained", activeCalls)
	}
	if _, err := client.open(client.callCtx, "third-session", "third-open-after-observer-loss"); err != nil {
		t.Fatalf("OpenSession after Gateway observer loss: %v", err)
	}
	waitSignal(t, opened, "new local execution session did not initialize after observer loss")
	r.mu.Lock()
	activeCalls = r.activeExecutionCalls
	r.mu.Unlock()
	if activeCalls != 3 {
		t.Fatalf("active execution calls after new OpenSession = %d, want 3", activeCalls)
	}
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
	executor := &blockingDirectExecutor{started: make(chan struct{}), release: make(chan struct{})}
	config := executionRuntimeConfig(t, "ws://unused", nil)
	config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
		return &executionServiceHandle{executor: executor, fence: func(context.Context) error { return nil }}, nil
	}
	r := newLifecycleRuntime(t, config)
	client := startDirectExecutionTestClient(t, r, "incarnation-1")
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

type failingAdmissionCountState struct {
	state.State
	failNext atomic.Bool
	failure  error
}

func (s *failingAdmissionCountState) UpdateWithConflicts(ctx context.Context, pointer resource.Pointer, update state.UpdaterFunc, options ...state.UpdateOption) (resource.Resource, error) {
	if pointer.Type() == lifecycle.AdmissionCountType && s.failNext.Swap(false) {
		return nil, s.failure
	}
	return s.State.UpdateWithConflicts(ctx, pointer, update, options...)
}

func TestDirectUnaryExecutionAdmissionFailureLeavesNoCall(t *testing.T) {
	executor := &blockingDirectExecutor{started: make(chan struct{}), release: make(chan struct{})}
	close(executor.release)
	config := executionRuntimeConfig(t, "ws://unused", nil)
	config.expectCloseError = true
	config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
		return &executionServiceHandle{executor: executor, fence: func(context.Context) error { return nil }}, nil
	}
	store := &failingAdmissionCountState{
		State:   state.WrapCore(namespaced.NewState(inmem.Build)),
		failure: status.Error(codes.Internal, "injected admission count write failure"),
	}
	r := newLifecycleRuntimeWithState(t, config, store)
	client := startDirectExecutionTestClient(t, r, "incarnation-1")
	request := &executorpb.StartProcessRequest{Scope: client.scope("unary-session")}
	store.failNext.Store(true)

	if _, err := client.executor.StartProcess(client.callCtx, request); status.Code(err) != codes.Internal || store.failNext.Load() {
		t.Fatalf("StartProcess with failed admission count write = %v, want injected failure", err)
	}
	if _, err := client.executor.StartProcess(client.callCtx, request); status.Code(err) != codes.Unavailable {
		t.Fatalf("StartProcess after failed admission count write = %v, want Unavailable", err)
	}
	select {
	case <-executor.started:
		t.Fatal("failed admission dispatched an execution call")
	default:
	}
	r.mu.Lock()
	admissions, calls, registered := r.activeAdmissions, r.activeExecutionCalls, len(r.execCalls)
	r.mu.Unlock()
	if admissions != 0 || calls != 0 || registered != 0 {
		t.Fatalf("failed admission retained work: active admissions=%d, active execution calls=%d, registered calls=%d", admissions, calls, registered)
	}
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
		{name: "missing target", mutate: func(scope *executorpb.ExecutionScope) { scope.Destination.Target = nil }, code: codes.InvalidArgument},
		{name: "foreign Sandbox", mutate: func(scope *executorpb.ExecutionScope) { scope.Destination.Target.Uid = "another-sandbox" }, code: codes.PermissionDenied},
		{name: "foreign Sandbox with long Session ID", mutate: func(scope *executorpb.ExecutionScope) {
			scope.Destination.Target.Uid = "another-sandbox"
			scope.SessionId = strings.Repeat("x", 129)
		}, code: codes.PermissionDenied},
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

func TestDirectExecutionAdmissionClosesOnBackendLossBeforeControllerEvent(t *testing.T) {
	backendLife, loseBackend := context.WithCancel(t.Context())
	executor := &blockingDirectExecutor{started: make(chan struct{}), release: make(chan struct{})}
	close(executor.release)
	config := executionRuntimeConfig(t, "ws://unused", nil)
	config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
		// There is deliberately no ProcessExit notification: admission must observe
		// the backend lifetime without waiting for a queued COSI loss event.
		return &executionServiceHandle{
			executor: executor, backendLife: backendLife,
			fence: func(context.Context) error { return nil },
		}, nil
	}
	r := newLifecycleRuntime(t, config)
	client := startDirectExecutionTestClient(t, r, "incarnation-1")
	loseBackend()
	_, err := client.executor.StartProcess(client.callCtx, &executorpb.StartProcessRequest{Scope: client.scope("lost-backend-session")})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("execution admission after backend loss: %v", err)
	}
	select {
	case <-executor.started:
		t.Fatal("backend loss dispatched a new execution call")
	default:
	}
}

func TestPassiveWatchCancellationPreservesAdmittedExecution(t *testing.T) {
	// This fixture explicitly selects insecure transport; the mTLS peer separation
	// is exercised by TestControllerPassiveWatchCoexistsWithGatewayAndCannotControl.
	executor := &blockingDirectExecutor{started: make(chan struct{}), release: make(chan struct{})}
	config := executionRuntimeConfig(t, "ws://unused", nil)
	config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
		return &executionServiceHandle{executor: executor, fence: func(context.Context) error { return nil }}, nil
	}
	r := newLifecycleRuntime(t, config)
	client := startDirectExecutionTestClient(t, r, "incarnation-1")
	completed := make(chan error, 1)
	go func() {
		_, err := client.executor.StartProcess(client.callCtx, &executorpb.StartProcessRequest{Scope: client.scope("passive-session")})
		completed <- err
	}()
	select {
	case <-executor.started:
	case err := <-completed:
		t.Fatalf("execution ended before dispatch: %v", err)
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("execution did not start")
	}
	watchCtx, cancelWatch := context.WithCancel(client.ctx)
	defer cancelWatch()
	watch, err := client.runtime.WatchState(watchCtx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: client.identity.UID, IncarnationUid: client.incarnation, ObserveOnly: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if snapshot, err := watch.Recv(); err != nil || snapshot.GetFacts() == nil {
		t.Fatalf("passive watch during execution = %v, error = %v", snapshot, err)
	}
	cancelWatch()
	if _, err := watch.Recv(); status.Code(err) != codes.Canceled {
		t.Fatalf("passive watch cancellation = %v", err)
	}
	close(executor.release)
	select {
	case err := <-completed:
		if err != nil {
			t.Fatalf("admitted execution after passive cancellation: %v", err)
		}
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("admitted execution did not complete")
	}
	if err := client.replaceWatch(client.incarnation); err != nil {
		t.Fatalf("Gateway watch after passive cancellation: %v", err)
	}
}

type observerIndependentExecutor struct {
	executorpb.UnimplementedExecutorServer
	release chan struct{}
}

func (e *observerIndependentExecutor) OpenSession(_ *executorpb.OpenSessionRequest, stream executorpb.Executor_OpenSessionServer) error {
	if err := stream.Send(&executorpb.OpenSessionResponse{ExecutionSessionId: "before-observer-change"}); err != nil {
		return err
	}
	select {
	case <-stream.Context().Done():
		return stream.Context().Err()
	case <-e.release:
		return stream.Send(&executorpb.OpenSessionResponse{ExecutionSessionId: "after-observer-change"})
	}
}

func TestExecutionSurvivesGatewayObserverLoss(t *testing.T) {
	executor := &observerIndependentExecutor{release: make(chan struct{})}
	config := executionRuntimeConfig(t, "ws://unused", nil)
	config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
		return &executionServiceHandle{executor: executor, fence: func(context.Context) error { return nil }}, nil
	}
	r := newLifecycleRuntime(t, config)
	client := startDirectExecutionTestClient(t, r, "incarnation")
	stream, err := client.open(client.callCtx, "observer-loss-session", "observer-loss-open")
	if err != nil {
		t.Fatal(err)
	}
	if response, err := stream.Recv(); err != nil || response.GetExecutionSessionId() != "before-observer-change" {
		t.Fatalf("initial execution response = %v, error=%v", response, err)
	}
	if err := client.stopWatch(); status.Code(err) != codes.Canceled {
		t.Fatalf("Gateway observer cancellation = %v, want Canceled", err)
	}
	if _, err := client.runtime.GetStatus(client.callCtx, &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("GetStatus after Gateway observer loss: %v", err)
	}
	close(executor.release)
	if response, err := stream.Recv(); err != nil || response.GetExecutionSessionId() != "after-observer-change" {
		t.Fatalf("execution response after observer loss = %v, error=%v", response, err)
	}
}

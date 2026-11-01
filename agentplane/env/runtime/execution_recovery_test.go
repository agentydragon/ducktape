package runtime

import (
	"context"
	"errors"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

type recoveryTestHandle struct {
	conn      *grpc.ClientConn
	exited    chan childExit
	fenced    chan struct{}
	fenceOnce sync.Once
	mu        sync.Mutex
	expected  bool
}

func (h *recoveryTestHandle) ClientConn() *grpc.ClientConn { return h.conn }
func (h *recoveryTestHandle) StopAndFence(context.Context) error {
	h.mu.Lock()
	h.expected = true
	h.mu.Unlock()
	h.fenceOnce.Do(func() { close(h.fenced) })
	return nil
}
func (h *recoveryTestHandle) Close() error                  { return h.conn.Close() }
func (h *recoveryTestHandle) ProcessExit() <-chan childExit { return h.exited }
func (h *recoveryTestHandle) stopRequested() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.expected
}

func recoveryTestConn(t *testing.T, target string) *grpc.ClientConn {
	t.Helper()
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	return conn
}

func TestFencedExecutionLossRestartsAPIForSameIncarnation(t *testing.T) {
	url, opened, _ := startNativeExecutionTestServer(t, false)
	config := executionRuntimeConfig(t, url, nil)
	originalStart := config.deps.startExecution
	starts := make(chan *localExecutionHandle, 2)
	events := make(chan string, 3)
	config.deps.startExecution = func(ctx context.Context, config Config, uid string) (executionHandle, error) {
		handle, err := originalStart(ctx, config, uid)
		if err != nil {
			return nil, err
		}
		local := handle.(*localExecutionHandle)
		local.exited = make(chan childExit, 2)
		if err := waitExecutionReady(ctx, local.ClientConn()); err != nil {
			_ = local.StopAndFence(context.Background())
			return nil, err
		}
		events <- "start"
		starts <- local
		return local, nil
	}
	r := newLifecycleRunner(t, config)
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	first := <-starts

	stream, err := client.open(client.callCtx, "session-before-loss", "open-before-loss")
	if err != nil {
		t.Fatalf("open before loss: %v", err)
	}
	response, err := stream.Recv()
	if err != nil || response.GetStatus().GetOutcome().String() != "OUTCOME_COMPLETED" {
		t.Fatalf("initial OpenSession response=%v, err=%v", response, err)
	}
	select {
	case <-opened:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("initial native executor session did not open")
	}
	if got := <-events; got != "start" {
		t.Fatalf("first lifecycle event=%q, want start", got)
	}

	events <- "fenced"
	first.loseBackendAfterFence()
	oldStreamDone := make(chan error, 1)
	go func() { _, err := stream.Recv(); oldStreamDone <- err }()
	select {
	case err := <-oldStreamDone:
		if err == nil {
			t.Fatal("lost Execution service left its old OpenSession stream active")
		}
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("old OpenSession stream did not settle after the fenced service exit")
	}

	var replacement *localExecutionHandle
	select {
	case replacement = <-starts:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Runtime did not start a replacement Execution API service")
	}
	if replacement == first {
		t.Fatal("Runtime reused the exited Execution API process")
	}
	if got := <-events; got != "fenced" {
		t.Fatalf("replacement ordering event=%q, want fenced", got)
	}
	if got := <-events; got != "start" {
		t.Fatalf("replacement ordering event=%q, want start", got)
	}
	select {
	case session := <-opened:
		t.Fatalf("Runtime replayed the prior Session request after recovery: %q", session)
	default:
	}

	newStream, err := client.open(client.callCtx, "session-after-loss", "open-after-loss")
	if err != nil {
		t.Fatalf("open after fenced recovery: %v", err)
	}
	response, err = newStream.Recv()
	if err != nil || response.GetStatus().GetOutcome().String() != "OUTCOME_COMPLETED" {
		t.Fatalf("new OpenSession response=%v, err=%v", response, err)
	}
	select {
	case <-opened:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("fresh operation did not reach the recovered Execution service")
	}
	_ = newStream.CloseSend()
}

func TestExecutionServiceUnexpectedNonzeroExitIsFatalWithoutReplacement(t *testing.T) {
	handle := &localExecutionHandle{exited: make(chan childExit, 2)}
	r := &Runtime{
		executionOwner: handle,
		executionUID:   "test-incarnation",
		intent: &runtimepb.LifecycleIntent{
			Mode: runtimepb.LifecycleIntent_RUNNING, IncarnationUid: "test-incarnation",
		},
		changes: make(chan struct{}), fatal: make(chan error, 1),
		deps: runtimeDeps{startExecution: func(context.Context, Config, string) (executionHandle, error) {
			return nil, errors.New("must not replace an unfenced process")
		}},
	}
	go r.watchExecutionProcess(handle, "test-incarnation", handle)
	handle.exitWithoutFence(1)
	select {
	case err := <-r.fatal:
		if err == nil {
			t.Fatal("nonzero Execution exit did not report a fatal error")
		}
	case <-time.After(time.Second):
		t.Fatal("Runtime did not fail after an Execution exit without fence proof")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.executionOwner != handle || r.executionUID != "test-incarnation" {
		t.Fatal("Runtime discarded the old Execution owner without fence proof")
	}
	if r.executionRecovering {
		t.Fatal("Runtime treated a nonzero exit as recoverable")
	}
}

func TestFencedExecutionLossDuringQuiesceDoesNotRestartAndClearsOwner(t *testing.T) {
	old := &recoveryTestHandle{
		conn:   recoveryTestConn(t, "passthrough:///127.0.0.1:1"),
		exited: make(chan childExit, 1), fenced: make(chan struct{}),
	}
	var starts atomic.Int32
	r := &Runtime{
		config:         Config{CodexExecServer: &codexexecserver.Config{URL: "ws://executor.invalid"}},
		executionOwner: old, executionConn: old.conn, executionUID: "test-incarnation",
		intent:  &runtimepb.LifecycleIntent{Mode: runtimepb.LifecycleIntent_QUIESCING, IncarnationUid: "test-incarnation"},
		changes: make(chan struct{}), fatal: make(chan error, 1),
		execCalls: make(map[uint64]context.CancelFunc), execStreams: make(map[uint64]context.CancelFunc),
		deps: runtimeDeps{startExecution: func(context.Context, Config, string) (executionHandle, error) {
			starts.Add(1)
			conn, err := grpc.NewClient("passthrough:///127.0.0.1:2", grpc.WithTransportCredentials(insecure.NewCredentials()))
			if err != nil {
				return nil, err
			}
			return &recoveryTestHandle{
				conn:   conn,
				fenced: make(chan struct{}),
			}, nil
		}},
	}
	go r.watchExecutionProcess(old, "test-incarnation", old)
	old.exited <- childExit{code: 0}
	select {
	case <-old.fenced:
	case <-time.After(time.Second):
		t.Fatal("Runtime did not fence the cleanly exited service while quiescing")
	}
	waitForExecutionOwner(t, r, nil)
	if got := starts.Load(); got != 0 {
		t.Fatalf("Runtime started %d replacements while quiescing; want none", got)
	}

	if err := r.startExecutionForUID(context.Background(), "test-incarnation"); err != nil {
		t.Fatalf("resume same incarnation after fenced quiesce: %v", err)
	}
	if got := starts.Load(); got != 1 {
		t.Fatalf("resume start count = %d, want one fresh service", got)
	}
	r.mu.Lock()
	got := r.executionOwner
	r.mu.Unlock()
	if got == nil || got == old {
		t.Fatal("same-incarnation resume reused the fenced service handle")
	}
	_ = got.Close()
}

func TestSystemdHealthLossFencesInvocationBeforeReplacement(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthServer.SetServingStatus(executorpb.Executor_ServiceDesc.ServiceName, healthpb.HealthCheckResponse_SERVING)
	grpcServer := grpc.NewServer()
	healthpb.RegisterHealthServer(grpcServer, healthServer)
	go func() { _ = grpcServer.Serve(listener) }()
	t.Cleanup(func() {
		grpcServer.Stop()
		_ = listener.Close()
	})
	target := "passthrough:///" + listener.Addr().String()
	oldConn := recoveryTestConn(t, target)
	readyCtx, cancelReady := context.WithTimeout(t.Context(), time.Second)
	if err := waitExecutionReady(readyCtx, oldConn); err != nil {
		cancelReady()
		t.Fatal(err)
	}
	cancelReady()

	events := make(chan string, 4)
	releaseFence := make(chan struct{})
	old := &supervisedExecutionHandle{conn: oldConn, exited: make(chan childExit, 1)}
	old.fence = func(ctx context.Context) error {
		events <- "fence-start"
		select {
		case <-releaseFence:
			events <- "fence-complete"
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	healthCtx, cancelHealth := context.WithCancel(context.Background())
	defer cancelHealth()
	old.healthCancel = cancelHealth
	go old.monitorHealth(healthCtx, 5*time.Millisecond, 100*time.Millisecond, 1)

	var starts atomic.Int32
	var replacementHandle *supervisedExecutionHandle
	r := &Runtime{
		config:         Config{CodexExecServer: &codexexecserver.Config{URL: "ws://executor.invalid"}},
		executionOwner: old, executionConn: oldConn, executionUID: "test-incarnation",
		intent:  &runtimepb.LifecycleIntent{Mode: runtimepb.LifecycleIntent_RUNNING, IncarnationUid: "test-incarnation"},
		changes: make(chan struct{}), fatal: make(chan error, 1),
		execCalls: make(map[uint64]context.CancelFunc), execStreams: make(map[uint64]context.CancelFunc),
		deps: runtimeDeps{startExecution: func(ctx context.Context, _ Config, _ string) (executionHandle, error) {
			starts.Add(1)
			healthServer.SetServingStatus(executorpb.Executor_ServiceDesc.ServiceName, healthpb.HealthCheckResponse_SERVING)
			conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
			if err != nil {
				return nil, err
			}
			if err := waitExecutionReady(ctx, conn); err != nil {
				_ = conn.Close()
				return nil, err
			}
			replacementHandle = &supervisedExecutionHandle{conn: conn, exited: make(chan childExit, 1)}
			events <- "replacement-ready"
			return replacementHandle, nil
		}},
	}
	go r.watchExecutionProcess(old, "test-incarnation", old)
	healthServer.SetServingStatus(executorpb.Executor_ServiceDesc.ServiceName, healthpb.HealthCheckResponse_NOT_SERVING)
	select {
	case event := <-events:
		if event != "fence-start" {
			t.Fatalf("first recovery event = %q, want fence-start", event)
		}
	case <-time.After(time.Second):
		t.Fatal("systemd Execution health monitor did not request an invocation fence")
	}
	r.mu.Lock()
	ownerBeforeFence := r.executionOwner
	r.mu.Unlock()
	if ownerBeforeFence != old || starts.Load() != 0 {
		t.Fatal("Runtime replaced or released the systemd owner before invocation fence proof")
	}
	close(releaseFence)
	select {
	case event := <-events:
		if event != "fence-complete" {
			t.Fatalf("event after fence release = %q, want fence-complete", event)
		}
	case <-time.After(time.Second):
		t.Fatal("systemd invocation fence did not complete")
	}
	select {
	case event := <-events:
		if event != "replacement-ready" {
			t.Fatalf("event after invocation fence = %q, want replacement-ready", event)
		}
	case <-time.After(time.Second):
		t.Fatal("Runtime did not wait for replacement Health readiness")
	}
	waitForExecutionOwner(t, r, replacementHandle)
	if got := starts.Load(); got != 1 {
		t.Fatalf("replacement count = %d, want one", got)
	}
	installCtx, cancelInstall := context.WithTimeout(t.Context(), time.Second)
	defer cancelInstall()
	for {
		r.mu.Lock()
		newOwner := r.executionOwner
		changed := r.changes
		r.mu.Unlock()
		if newOwner == replacementHandle {
			break
		}
		if newOwner != nil && newOwner != old {
			t.Fatalf("Runtime installed an unexpected execution handle %T", newOwner)
		}
		select {
		case <-changed:
		case <-installCtx.Done():
			t.Fatal("Runtime did not install the healthy replacement handle")
		}
	}
	r.mu.Lock()
	newOwner := r.executionOwner
	r.mu.Unlock()
	if newOwner == nil || newOwner == old {
		t.Fatal("Runtime did not install the healthy replacement handle")
	}
	_ = newOwner.Close()
}

func TestSystemdHealthLossFenceFailureRetainsOwnerAndIsFatal(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	healthServer := health.NewServer()
	healthServer.SetServingStatus(executorpb.Executor_ServiceDesc.ServiceName, healthpb.HealthCheckResponse_SERVING)
	grpcServer := grpc.NewServer()
	healthpb.RegisterHealthServer(grpcServer, healthServer)
	go func() { _ = grpcServer.Serve(listener) }()
	t.Cleanup(func() {
		grpcServer.Stop()
		_ = listener.Close()
	})
	conn := recoveryTestConn(t, "passthrough:///"+listener.Addr().String())
	t.Cleanup(func() { _ = conn.Close() })
	readyCtx, cancelReady := context.WithTimeout(t.Context(), time.Second)
	if err := waitExecutionReady(readyCtx, conn); err != nil {
		cancelReady()
		t.Fatal(err)
	}
	cancelReady()

	fenceFailure := errors.New("systemd invocation cgroup remained populated")
	old := &supervisedExecutionHandle{conn: conn, exited: make(chan childExit, 1), fence: func(context.Context) error {
		return fenceFailure
	}}
	healthCtx, cancelHealth := context.WithCancel(context.Background())
	defer cancelHealth()
	old.healthCancel = cancelHealth
	go old.monitorHealth(healthCtx, 5*time.Millisecond, 100*time.Millisecond, 1)
	starts := atomic.Int32{}
	r := &Runtime{
		config:         Config{CodexExecServer: &codexexecserver.Config{URL: "ws://executor.invalid"}},
		executionOwner: old, executionConn: conn, executionUID: "test-incarnation",
		intent:  &runtimepb.LifecycleIntent{Mode: runtimepb.LifecycleIntent_RUNNING, IncarnationUid: "test-incarnation"},
		changes: make(chan struct{}), fatal: make(chan error, 1),
		execCalls: make(map[uint64]context.CancelFunc), execStreams: make(map[uint64]context.CancelFunc),
		deps: runtimeDeps{startExecution: func(context.Context, Config, string) (executionHandle, error) {
			starts.Add(1)
			return nil, errors.New("must not replace an unfenced systemd invocation")
		}},
	}
	go r.watchExecutionProcess(old, "test-incarnation", old)
	healthServer.SetServingStatus(executorpb.Executor_ServiceDesc.ServiceName, healthpb.HealthCheckResponse_NOT_SERVING)
	select {
	case err := <-r.fatal:
		if !errors.Is(err, fenceFailure) {
			t.Fatalf("Runtime failure = %v, want exact invocation fence failure %v", err, fenceFailure)
		}
	case <-time.After(time.Second):
		t.Fatal("Runtime did not fail after systemd invocation fence failed")
	}
	r.mu.Lock()
	owner, uid := r.executionOwner, r.executionUID
	r.mu.Unlock()
	if owner != old || uid != "test-incarnation" {
		t.Fatal("Runtime released systemd ownership without process fence proof")
	}
	if got := starts.Load(); got != 0 {
		t.Fatalf("Runtime started %d replacements after fence failure, want none", got)
	}
}

func waitForExecutionOwner(t *testing.T, r *Runtime, want executionHandle) {
	t.Helper()
	deadline := time.NewTimer(time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		r.mu.Lock()
		owner, recovering := r.executionOwner, r.executionRecovering
		r.mu.Unlock()
		if owner == want && !recovering {
			return
		}
		select {
		case <-ticker.C:
		case <-deadline.C:
			t.Fatalf("Execution owner/recovery did not settle: owner=%T recovering=%v", owner, recovering)
		}
	}
}

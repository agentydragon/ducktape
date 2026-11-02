package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type failingCloseHostLifetime struct {
	hostLifetime
	stops atomic.Int32
}

type uncertainCloseExecution struct{ fences atomic.Int32 }

func (*uncertainCloseExecution) Executor() executorpb.ExecutorServer {
	return &executorpb.UnimplementedExecutorServer{}
}

func (e *uncertainCloseExecution) StopAndFence(context.Context) error {
	e.fences.Add(1)
	return errors.New("uncertain Execution backend fence")
}

func TestRuntimeCloseUncertainExecutionFenceRetainsOwner(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	execution := &uncertainCloseExecution{}
	config.CodexExecServer = &codexexecserver.Config{URL: "ws://unused"}
	config.CodexBinary = "/bin/false"
	config.HarnessEnvironment = map[string]string{}
	config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
		return execution, nil
	}
	r := newLifecycleRunner(t, config)
	r.mu.Lock()
	owned := r.executionOwner.handle
	r.mu.Unlock()
	if owned != execution {
		t.Fatal("Execution service was not installed through its COSI lifecycle")
	}

	for range 2 {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		err := r.Close(ctx)
		cancel()
		if err == nil || !strings.Contains(err.Error(), "uncertain Execution backend fence") {
			t.Fatalf("Close = %v, want uncertain fence error", err)
		}
	}
	if got := execution.fences.Load(); got != 1 {
		t.Fatalf("Execution fence attempts = %d, want one", got)
	}
	status, found, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
	if err != nil || !found || status.Stage != lifecycle.RuntimeCloseFailed {
		t.Fatalf("Close aggregate = %+v, found=%t, err=%v", status, found, err)
	}
	if owner, err := openEnvironmentOwner(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("uncertain Execution fence released environment owner")
	}
}

func TestRuntimeCloseRetainsOwnerAfterRejectedLiveExecutionStart(t *testing.T) {
	for _, closeFirst := range []bool{false, true} {
		name := "rejection-before-close"
		if closeFirst {
			name = "close-started-before-rejection"
		}
		t.Run(name, func(t *testing.T) {
			proxy := modelConfigServer(t)
			config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
			config.expectCloseError = true
			first := newControllerExecutionHandle(nil)
			config.CodexExecServer = &codexexecserver.Config{URL: "ws://unused"}
			config.CodexBinary = "/bin/false"
			config.HarnessEnvironment = map[string]string{}
			config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
				return first, nil
			}
			r := newLifecycleRunner(t, config)
			current, found, err := cosiExecutionStatus(r)
			if err != nil || !found || current.Phase != executionReady {
				t.Fatalf("Execution before rejected start = %+v, found=%t, err=%v", current, found, err)
			}
			orphan := newControllerExecutionHandle(nil)
			reject := func() {
				r.executionLifecycle.completeStart(current.Generation, current.UID, current.Demand, orphan, t.Context(), nil, false)
			}
			closeCtx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
			defer cancel()
			var closeErr error
			if closeFirst {
				// Close has set its closing bit, but its worker cannot finish
				// before the rejected handle makes the lifecycle terminal.
				r.lifecycleStartMu.Lock()
				locked := true
				defer func() {
					if locked {
						r.lifecycleStartMu.Unlock()
					}
				}()
				closeDone := make(chan error, 1)
				go func() { closeDone <- r.Close(closeCtx) }()
				waitRuntime(t, r, func() bool {
					r.mu.Lock()
					defer r.mu.Unlock()
					return r.closing
				}, "Runtime Close to start before rejected Execution completion")
				reject()
				r.lifecycleStartMu.Unlock()
				locked = false
				closeErr = <-closeDone
			} else {
				reject()
				closeErr = r.Close(closeCtx)
			}
			if closeErr == nil || !strings.Contains(closeErr.Error(), "start handle has no published completion") {
				t.Fatalf("Close after rejected live start = %v", closeErr)
			}
			r.executionLifecycle.mu.Lock()
			retained := len(r.executionLifecycle.unfencedStarts) == 1 && r.executionLifecycle.unfencedStarts[0] == orphan
			r.executionLifecycle.mu.Unlock()
			r.mu.Lock()
			owned := r.executionOwner != nil && r.executionOwner.handle == first && !r.executionOwner.admitted
			r.mu.Unlock()
			if !retained || !owned {
				t.Fatalf("Close lost native handles: original owner retained=%t, rejected handle retained=%t", owned, retained)
			}
			if owner, err := openEnvironmentOwner(config.StateDir); err == nil {
				_ = owner.Close()
				t.Fatal("Close released environment owner after rejected live Execution start")
			}
		})
	}
}

type heldCloseExecution struct {
	fences  atomic.Int32
	started chan struct{}
	release chan struct{}
}

func (*heldCloseExecution) Executor() executorpb.ExecutorServer {
	return &executorpb.UnimplementedExecutorServer{}
}

func (e *heldCloseExecution) StopAndFence(ctx context.Context) error {
	if e.fences.Add(1) == 1 {
		close(e.started)
	}
	select {
	case <-e.release:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func TestRuntimeCloseJoinsInFlightAutomaticExecutionFence(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	execution := &heldCloseExecution{started: make(chan struct{}), release: make(chan struct{})}
	config.CodexExecServer = &codexexecserver.Config{URL: "ws://unused"}
	config.CodexBinary = "/bin/false"
	config.HarnessEnvironment = map[string]string{}
	config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
		return execution, nil
	}
	r := newLifecycleRunner(t, config)
	defer func() {
		select {
		case <-execution.release:
		default:
			close(execution.release)
		}
	}()
	if _, err := r.Quiesce(context.Background(), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-execution.started:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("automatic Execution fence did not start")
	}
	r.mu.Lock()
	binding := proto.Clone(r.intent.Binding).(*runtimepb.Binding)
	r.mu.Unlock()
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, func() bool {
		status, found, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
		return err == nil && found && status.Stage == lifecycle.RuntimeCloseExecutionFencing
	}, "Close to join in-flight Execution fence")
	r.mu.Lock()
	sameBinding := r.intent != nil && proto.Equal(r.intent.Binding, binding)
	r.mu.Unlock()
	if !sameBinding {
		t.Fatal("Close replaced the draining Host generation")
	}
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned before fence completed: %v", err)
	default:
	}
	close(execution.release)
	select {
	case err := <-closeDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Close did not finish after the Execution fence")
	}
	if got := execution.fences.Load(); got != 1 {
		t.Fatalf("Execution fence attempts = %d, want one", got)
	}
}

func (h *failingCloseHostLifetime) Stop(ctx context.Context) error {
	h.stops.Add(1)
	return errors.Join(h.hostLifetime.Stop(ctx), errors.New("injected Host join failure"))
}

func TestRuntimeCloseFailureRetainsOwnerAndDoesNotRetryHostStop(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	r := newLifecycleRunner(t, config)
	host := &failingCloseHostLifetime{hostLifetime: r.hostLifetime}
	r.mu.Lock()
	r.hostLifetime = host
	r.mu.Unlock()

	for range 2 {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		err := r.Close(ctx)
		cancel()
		if err == nil || !strings.Contains(err.Error(), "injected Host join failure") {
			t.Fatalf("Close = %v, want exact Host join failure", err)
		}
	}
	if got := host.stops.Load(); got != 1 {
		t.Fatalf("Host stop calls = %d, want one", got)
	}
	status, found, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
	if err != nil || !found || status.Stage != lifecycle.RuntimeCloseFailed || !strings.Contains(status.Error, "injected Host join failure") {
		t.Fatalf("Close aggregate = %+v, found=%t, err=%v", status, found, err)
	}
	if owner, err := openEnvironmentOwner(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("failed Host join released environment owner")
	}
}

type failedRuntimeWatchState struct {
	state.State
	err error
}

func (s *failedRuntimeWatchState) WatchKindAggregated(context.Context, resource.Kind, chan<- []state.Event, ...state.WatchKindOption) error {
	return s.err
}

func TestRuntimeRetainsOwnerAfterLifecycleEngineFailure(t *testing.T) {
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	failure := errors.New("injected shared lifecycle watch failure")
	store := &failedRuntimeWatchState{State: state.WrapCore(namespaced.NewState(inmem.Build)), err: failure}
	r, err := newRuntime(config.Config, store, runtimeDeps{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = r.owner.Close() })
	r.startLifecycleEngine()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	select {
	case <-r.lifecycleDone:
	case <-ctx.Done():
		t.Fatal("failing lifecycle engine did not stop")
	}
	if !errors.Is(r.lifecycleContext.Err(), context.Canceled) {
		t.Fatalf("process host lifetime after engine failure = %v, want canceled", r.lifecycleContext.Err())
	}
	if err := r.Close(ctx); !errors.Is(err, failure) {
		t.Fatalf("Runtime Close after engine failure = %v", err)
	}
	owner, err := openEnvironmentOwner(config.StateDir)
	if err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released owner after lifecycle engine failed")
	}
}

func TestRuntimeCloseBeforeRunStartsAndJoinsLifecycleEngine(t *testing.T) {
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	r, err := newRuntime(config.Config, nil, runtimeDeps{})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := r.Close(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-r.lifecycleDone:
	default:
		t.Fatal("Runtime released owner before joining lifecycle engine")
	}
	owner, err := openEnvironmentOwner(config.StateDir)
	if err != nil {
		t.Fatalf("closed Runtime retained environment owner: %v", err)
	}
	_ = owner.Close()
}

func TestEmbeddedHostAndRuntimeKeepDistinctDesiredResources(t *testing.T) {
	proxy := modelConfigServer(t)
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false"))
	runtimeDesired, runtimeFound, err := lifecycle.ReadEnvironmentDesired(context.Background(), r.lifecycleState)
	if err != nil || !runtimeFound {
		t.Fatalf("Runtime desired resource: found=%t err=%v", runtimeFound, err)
	}
	hostDesired, hostFound, err := lifecycle.ReadDesired(context.Background(), r.lifecycleState)
	if err != nil || !hostFound {
		t.Fatalf("Host desired resource: found=%t err=%v", hostFound, err)
	}
	if runtimeDesired.Mode != lifecycle.Running || hostDesired.Mode != lifecycle.Running || runtimeDesired.Generation != hostDesired.Generation {
		t.Fatalf("Runtime desired=%+v Host desired=%+v", runtimeDesired, hostDesired)
	}
}

type failingHostCleanup struct {
	hostLifetime
	err error
}

func (f failingHostCleanup) Cleanup(ctx context.Context) error {
	return errors.Join(f.hostLifetime.Cleanup(ctx), f.err)
}

func TestRuntimeRetainsOwnerWhenJoinedHostCleanupFails(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	r := newLifecycleRunner(t, config)
	cleanupFailure := errors.New("injected Host owner release failure")
	r.hostLifetime = failingHostCleanup{hostLifetime: r.hostLifetime, err: cleanupFailure}
	t.Cleanup(func() { _ = r.owner.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := r.Close(ctx); !errors.Is(err, cleanupFailure) {
		t.Fatalf("Runtime Close = %v, want Host cleanup failure", err)
	}
	owner, err := openEnvironmentOwner(config.StateDir)
	if err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released environment owner without Host cleanup proof")
	}
}

func TestCloseCannotStartEngineDuringFailedHostConstruction(t *testing.T) {
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	config.Config.HostConfig = config.host
	config.Config.HostConfig.HarnessEnvironment = map[string]string{"CODEX_EXEC_SERVER_URL": "invalid-host-config"}
	entered := make(chan struct{})
	release := make(chan struct{})
	config.deps.beforeHostConstruction = func() {
		close(entered)
		<-release
	}
	r, err := newRuntime(config.Config, nil, config.deps)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = r.owner.Close() })
	runDone := make(chan error, 1)
	go func() { runDone <- r.Run(context.Background()) }()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("Host construction did not start")
	}
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(ctx) }()
	waitCtx, stopWaiting := context.WithCancel(ctx)
	canceledWaiter := make(chan error, 1)
	go func() { canceledWaiter <- r.Close(waitCtx) }()
	stopWaiting()
	select {
	case err := <-canceledWaiter:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("canceled Close waiter = %v", err)
		}
	case <-ctx.Done():
		t.Fatal("canceled Close waiter blocked on Host construction")
	}
	select {
	case err := <-closeDone:
		t.Fatalf("Close crossed Host construction barrier: %v", err)
	default:
	}
	close(release)
	select {
	case err := <-runDone:
		if err == nil {
			t.Fatal("Run accepted invalid Host configuration")
		}
	case <-ctx.Done():
		t.Fatal("Run did not return failed Host construction")
	}
	select {
	case err := <-closeDone:
		if err == nil {
			t.Fatal("Close accepted partial Host construction")
		}
	case <-ctx.Done():
		t.Fatal("Close did not return failed Host construction")
	}
	select {
	case <-r.lifecycleDone:
		t.Fatal("Runtime started partially constructed lifecycle engine")
	default:
	}
	owner, err := openEnvironmentOwner(config.StateDir)
	if err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released owner after unsafe Host construction")
	}
}

func TestCloseDuringSuccessfulHostConstructionJoinsSharedEngine(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.Config.HostConfig = config.host
	config.Config.HostIngress.Target = startAcknowledgingHistory(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	config.deps.beforeHostConstruction = func() {
		close(entered)
		<-release
	}
	r, err := newRuntime(config.Config, nil, config.deps)
	if err != nil {
		t.Fatal(err)
	}
	runDone := make(chan error, 1)
	go func() { runDone <- r.Run(context.Background()) }()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("Host construction did not start")
	}
	expired, cancelExpired := context.WithCancel(ctx)
	cancelExpired()
	if err := r.Close(expired); !errors.Is(err, context.Canceled) {
		t.Fatalf("Close waiter during construction = %v", err)
	}
	close(release)
	if err := r.Close(ctx); err != nil && !strings.Contains(err.Error(), "Harness Host lifecycle connection was lost") {
		t.Fatalf("Close after Host construction = %v", err)
	}
	select {
	case <-r.lifecycleDone:
	default:
		t.Fatal("Runtime released owner before shared engine completed")
	}
	select {
	case <-runDone:
	case <-ctx.Done():
		t.Fatal("Run did not return after concurrent Close")
	}
	owner, err := openEnvironmentOwner(config.StateDir)
	if err != nil {
		t.Fatalf("joined Runtime retained owner: %v", err)
	}
	_ = owner.Close()
}

func TestCloseCancelsBlockedAdoptionWithoutStartingSuccessor(t *testing.T) {
	config := executionRuntimeConfig(t, "ws://unused", nil)
	start := config.deps.startExecution
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	defer once.Do(func() { close(release) })
	var mu sync.Mutex
	var starts []string
	config.deps.startExecution = func(ctx context.Context, config Config, uid string) (executionHandle, error) {
		mu.Lock()
		starts = append(starts, uid)
		mu.Unlock()
		handle, err := start(ctx, config, uid)
		if err != nil {
			return nil, err
		}
		return &recordingExecutionHandle{executionHandle: handle, beforeFence: func(ctx context.Context) error {
			if uid == "test-incarnation" {
				close(entered)
				select {
				case <-release:
				case <-ctx.Done():
					return ctx.Err()
				}
			}
			return nil
		}}, nil
	}
	r := newLifecycleRunner(t, config)
	adoptionDone := make(chan error, 1)
	go func() { adoptionDone <- r.adoptIncarnation(context.Background(), "successor") }()
	select {
	case <-entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("old incarnation fence was not requested")
	}
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, r.isClosing, "Close admission gate")
	once.Do(func() { close(release) })
	if err := waitClose(t, adoptionDone); status.Code(err) != codes.Canceled {
		t.Fatalf("canceled adoption = %v, want context cancellation", err)
	}
	if err := waitClose(t, closeDone); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	for _, uid := range starts {
		if uid == "successor" {
			t.Fatalf("successor Execution started after Close: %v", starts)
		}
	}
	if uid, ok := r.state.IncarnationUID(); !ok || uid != "test-incarnation" {
		t.Fatalf("Close changed incarnation during canceled adoption: %q (%t)", uid, ok)
	}
}

func TestEngineExitDuringCanceledExecutionStartJoinsTransitionAndRetainsOwner(t *testing.T) {
	config := executionRuntimeConfig(t, "ws://unused", nil)
	config.expectCloseError = true
	start := config.deps.startExecution
	entered := make(chan struct{})
	config.deps.startExecution = func(ctx context.Context, config Config, uid string) (executionHandle, error) {
		if uid == "successor" {
			close(entered)
			<-ctx.Done()
			return nil, ctx.Err()
		}
		return start(ctx, config, uid)
	}
	r := newLifecycleRunner(t, config)
	adoptionDone := make(chan error, 1)
	go func() { adoptionDone <- r.adoptIncarnation(context.Background(), "successor") }()
	select {
	case <-entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("successor Execution start was not admitted")
	}
	r.lifecycleCancel()
	select {
	case <-r.lifecycleDone:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Runtime lifecycle engine did not join its transition worker")
	}
	if err := waitClose(t, adoptionDone); err == nil {
		t.Fatal("adoption succeeded after lifecycle engine exit")
	}
	r.mu.Lock()
	unsafe := r.transitionUnsafe
	r.mu.Unlock()
	if unsafe == nil {
		t.Fatal("engine exit did not record the unproved Execution fence")
	}
	if owner, err := openEnvironmentOwner(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("engine exit released environment owner without proving Execution fence")
	}
}

func TestFreshSessionAdmissionInvalidatesIdleToken(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	before, err := r.facts()
	if err != nil || before.GetIdleSince() == nil {
		t.Fatalf("initial idle facts=%v, %v", before, err)
	}
	if _, err := client.runner.OpenSession(client.callCtx, openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Quiesce(context.Background(), &sessionpb.QuiesceRequest{IfIdleSince: before.IdleSince}); status.Code(err) != codes.Aborted {
		t.Fatalf("stale idle conditional=%v", err)
	}
	waitRuntime(t, r, func() bool { facts, err := r.facts(); return err == nil && facts.IdleSince != nil }, "admitted Session idle")
	after, err := r.facts()
	if err != nil || after.GetIdleSince() == nil || proto.Equal(before.IdleSince, after.IdleSince) {
		t.Fatalf("admission did not invalidate idle token: before=%v after=%v error=%v", before, after, err)
	}
	if _, err := r.Quiesce(context.Background(), &sessionpb.QuiesceRequest{IfIdleSince: after.IdleSince}); err != nil {
		t.Fatal(err)
	}
	request, found, err := lifecycle.ReadRuntimeTransitionRequest(context.Background(), r.lifecycleState)
	if err != nil || !found || !request.HasIdleSince || request.IdleSinceMicros != after.IdleSince.AsTime().UnixMicro() {
		t.Fatalf("conditional transition request = %+v, found=%v, err=%v", request, found, err)
	}
}

func TestEmbeddedHostLifecycleLossStillFencesExecution(t *testing.T) {
	url, _, _ := startNativeExecutionTestServer(t, false)
	config := executionRuntimeConfig(t, url, nil)
	config.expectCloseError = true
	started := make(chan *localExecutionHandle, 1)
	startExecution := config.deps.startExecution
	config.deps.startExecution = func(ctx context.Context, cfg Config, uid string) (executionHandle, error) {
		handle, err := startExecution(ctx, cfg, uid)
		if err == nil {
			started <- handle.(*localExecutionHandle)
		}
		return handle, err
	}
	r := newLifecycleRunner(t, config)
	var execution *localExecutionHandle
	select {
	case execution = <-started:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Execution service did not start")
	}
	r.mu.Lock()
	host := r.host
	r.mu.Unlock()
	r.disconnectHost(host, errors.New("simulated Host lifecycle loss"))
	closeCtx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := r.Close(closeCtx); err == nil {
		t.Fatal("Runtime silently accepted lost Host lifecycle")
	}
	if !execution.stopRequested() {
		t.Fatal("Runtime skipped Execution fence after Host lifecycle loss")
	}
}

func TestRepeatedQuiesceKeepsCurrentLifecycleGeneration(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	facts, err := r.facts()
	if err != nil || facts.GetIdleSince() == nil {
		t.Fatalf("initial idle facts=%v, %v", facts, err)
	}
	request := &sessionpb.QuiesceRequest{IfIdleSince: facts.IdleSince}
	if _, err := r.Quiesce(client.ctx, request); err != nil {
		t.Fatal(err)
	}
	firstStatus, found, err := lifecycle.ReadRuntimeTransitionStatus(context.Background(), r.lifecycleState)
	if err != nil || !found || firstStatus.Stage != lifecycle.RuntimeTransitionDone {
		t.Fatalf("first Quiesce transition = %+v, found=%t, err=%v", firstStatus, found, err)
	}
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.host != nil && r.intent != nil && r.hostReportLocked() != nil && r.hostReportLocked().Ready && proto.Equal(r.hostReportLocked().Binding, r.intent.Binding)
	}, "current quiesce lifecycle report")
	r.mu.Lock()
	desired := r.desired
	binding := proto.Clone(r.intent.Binding).(*runtimepb.Binding)
	r.mu.Unlock()
	if _, err := r.Quiesce(client.ctx, request); err != nil {
		t.Fatal(err)
	}
	secondStatus, found, err := lifecycle.ReadRuntimeTransitionStatus(context.Background(), r.lifecycleState)
	if err != nil || !found || secondStatus.Stage != lifecycle.RuntimeTransitionDone || secondStatus.Sequence <= firstStatus.Sequence {
		t.Fatalf("repeated Quiesce transition = %+v, found=%t, err=%v; first=%+v", secondStatus, found, err, firstStatus)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.desired != desired || !proto.Equal(r.intent.Binding, binding) {
		t.Fatalf("repeated Quiesce changed lifecycle generation: desired=%+v want=%+v, binding=%v want=%v", r.desired, desired, r.intent.Binding, binding)
	}
}

func TestBootstrapAndSetupCompleteOnCloseAndDoNotRerun(t *testing.T) {
	root := t.TempDir()
	workspace := t.TempDir()
	bootstrapStarted := filepath.Join(root, "bootstrap-started")
	bootstrapRelease := filepath.Join(root, "bootstrap-release")
	bootstrapCount := filepath.Join(root, "bootstrap-count")
	setupStarted := filepath.Join(root, "setup-started")
	setupRelease := filepath.Join(root, "setup-release")
	setupCount := filepath.Join(root, "setup-count")
	bootstrapScript := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf b >> %s", shellQuote(bootstrapStarted), shellQuote(bootstrapRelease), shellQuote(bootstrapCount))
	setup := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf s >> %s", shellQuote(setupStarted), shellQuote(setupRelease), shellQuote(setupCount))
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, &bootstrapScript, "/bin/false")
	config.StateDir = root
	r := newLifecycleRunner(t, config)
	t.Cleanup(func() { _ = os.WriteFile(bootstrapRelease, nil, 0600); _ = os.WriteFile(setupRelease, nil, 0600) })
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	request := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	request.SetupScript = &setup
	if _, err := client.runner.OpenSession(client.callCtx, request); err != nil {
		t.Fatal(err)
	}
	waitForFile(t, bootstrapStarted)
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, r.isClosing, "Runtime close intent")
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned during bootstrap: %v", err)
	default:
	}
	if err := os.WriteFile(bootstrapRelease, nil, 0600); err != nil {
		t.Fatal(err)
	}
	waitForFile(t, setupStarted)
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned during accepted setup: %v", err)
	default:
	}
	if err := os.WriteFile(setupRelease, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := waitClose(t, closeDone); err != nil {
		t.Fatal(err)
	}
	for path, want := range map[string]string{bootstrapCount: "b", setupCount: "s"} {
		data, err := os.ReadFile(path)
		if err != nil || string(data) != want {
			t.Fatalf("effect %s=%q, %v", path, data, err)
		}
	}
	restarted := newLifecycleRunner(t, config)
	successor := startDirectExecutionTestClient(t, restarted, "test-incarnation")
	if _, err := successor.runner.OpenSession(successor.callCtx, request); err != nil {
		t.Fatal(err)
	}
	sessions, err := successor.runner.ListSessions(successor.callCtx, &sessionpb.ListSessionsRequest{})
	if err != nil || len(sessions.GetSessions()) != 1 || sessions.Sessions[0].SetupState != sessionpb.SetupState_SETUP_STATE_SUCCEEDED {
		t.Fatalf("recovered setup=%v, %v", sessions, err)
	}
	waitRuntime(t, restarted, func() bool {
		facts, err := restarted.facts()
		return err == nil && facts.Bootstrap == environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED
	}, "durable bootstrap result")
	for path, want := range map[string]string{bootstrapCount: "b", setupCount: "s"} {
		data, err := os.ReadFile(path)
		if err != nil || string(data) != want {
			t.Fatalf("recovery replayed effect %s=%q, %v", path, data, err)
		}
	}
}

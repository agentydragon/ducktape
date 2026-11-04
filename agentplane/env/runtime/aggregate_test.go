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
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type uncertainCloseExecution struct{ fences atomic.Int32 }

type nativeStopReadProbe struct {
	state.State
	value resource.Resource
	err   error
	onGet func()
}

func (p *nativeStopReadProbe) Get(ctx context.Context, ptr resource.Pointer, opts ...state.GetOption) (resource.Resource, error) {
	if ptr.Type() == lifecycle.AggregateStatusType {
		if p.onGet != nil {
			p.onGet()
		}
		if p.err != nil {
			return nil, p.err
		}
		return p.value.DeepCopy(), nil
	}
	return p.State.Get(ctx, ptr, opts...)
}

func TestCloseNativeStopObservationPreservesStatusAndDeadlineOrdering(t *testing.T) {
	config := lifecycleConfig(t, modelConfigServer(t).URL, t.TempDir(), nil, "/bin/false")
	r := newLifecycleRuntime(t, config)
	ctx := t.Context()
	waitRuntime(t, r, func() bool {
		_, found, err := lifecycle.ReadStatus(ctx, r.lifecycleState)
		return err == nil && found
	}, "initial aggregate Session status")
	ptr := resource.NewMetadata(lifecycle.Namespace, lifecycle.AggregateStatusType, lifecycle.ResourceID, resource.VersionUndefined)
	base, err := r.lifecycleState.Get(ctx, ptr)
	if err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	generation := r.desired.Generation
	r.mu.Unlock()
	statusValue := func(status lifecycle.Status) resource.Resource {
		value := base.DeepCopy()
		*value.(interface{ TypedSpec() *lifecycle.Status }).TypedSpec() = status
		return value
	}
	expired, cancel := context.WithDeadline(ctx, time.Now().Add(-time.Second))
	defer cancel()
	failure := errors.New("aggregate status read failed")
	for _, tc := range []struct {
		name            string
		status          lifecycle.Status
		readErr         error
		fenceCtx        context.Context
		generation      uint64
		sessionsStarted bool
		wantSettled     bool
		wantErr         error
		wantCode        codes.Code
	}{
		{name: "pending", status: lifecycle.Status{Generation: generation}, fenceCtx: ctx, generation: generation, sessionsStarted: true},
		{name: "already stopped at deadline", status: lifecycle.Status{Generation: generation, NativeStopped: true}, fenceCtx: expired, generation: generation, sessionsStarted: true, wantSettled: true},
		{name: "stale status", status: lifecycle.Status{Generation: generation - 1, NativeStopped: true}, fenceCtx: ctx, generation: generation, sessionsStarted: true},
		{name: "aggregate error before deadline", status: lifecycle.Status{Generation: generation, Error: "native cleanup failed"}, fenceCtx: expired, generation: generation, sessionsStarted: true, wantSettled: true},
		{name: "status read error before deadline", readErr: failure, fenceCtx: expired, generation: generation, sessionsStarted: true, wantSettled: true, wantErr: failure},
		{name: "generation superseded before read error", readErr: failure, fenceCtx: expired, generation: generation - 1, sessionsStarted: true, wantSettled: true, wantCode: codes.Aborted},
		{name: "deadline without status", status: lifecycle.Status{Generation: generation}, fenceCtx: expired, generation: generation, sessionsStarted: true, wantSettled: true, wantErr: context.DeadlineExceeded},
		{name: "no Session component", readErr: failure, fenceCtx: expired, generation: generation, wantSettled: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			probe := &nativeStopReadProbe{State: r.lifecycleState, value: statusValue(tc.status), err: tc.readErr}
			c := &runtimeCloseController{r: r, plan: closePlan{generation: tc.generation, sessionsStarted: tc.sessionsStarted, fenceCtx: tc.fenceCtx}}
			settled, err := c.nativeStopSettled(ctx, probe)
			if settled != tc.wantSettled {
				t.Fatalf("settled = %t, want %t; err=%v", settled, tc.wantSettled, err)
			}
			if tc.wantCode != codes.OK {
				if status.Code(err) != tc.wantCode {
					t.Fatalf("error code = %v, want %v", status.Code(err), tc.wantCode)
				}
			} else if tc.status.Error != "" {
				if err == nil || err.Error() != tc.status.Error {
					t.Fatalf("aggregate error = %v, want %q", err, tc.status.Error)
				}
			} else if !errors.Is(err, tc.wantErr) {
				t.Fatalf("error = %v, want %v", err, tc.wantErr)
			}
		})
	}

	entered := make(chan struct{})
	release := make(chan struct{})
	probe := &nativeStopReadProbe{State: r.lifecycleState, value: statusValue(lifecycle.Status{Generation: generation})}
	probe.onGet = func() { close(entered); <-release }
	c := &runtimeCloseController{r: r, plan: closePlan{generation: generation, sessionsStarted: true, fenceCtx: expired}}
	result := make(chan struct {
		settled bool
		err     error
	}, 1)
	go func() {
		settled, err := c.nativeStopSettled(ctx, probe)
		result <- struct {
			settled bool
			err     error
		}{settled, err}
	}()
	select {
	case <-entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Close did not enter aggregate status read")
	}
	probe.value.(interface{ TypedSpec() *lifecycle.Status }).TypedSpec().NativeStopped = true
	close(release)
	select {
	case got := <-result:
		if !got.settled || got.err != nil {
			t.Fatalf("status settled during read at deadline = (%t, %v), want settled", got.settled, got.err)
		}
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Close did not finish aggregate status observation")
	}
}

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
	r := newLifecycleRuntime(t, config)
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
	progress := mustRuntimeCloseStatus(t, r)
	if progress.Stage != runtimeCloseFailed {
		t.Fatalf("Close progress = %+v", progress)
	}
	if owner, err := openTestRuntimeStore(config.StateDir); err == nil {
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
			r := newLifecycleRuntime(t, config)
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
			if owner, err := openTestRuntimeStore(config.StateDir); err == nil {
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
	r := newLifecycleRuntime(t, config)
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
	generation := r.desired.Generation
	r.mu.Unlock()
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, r.isClosing, "Close to join in-flight Execution fence")
	r.mu.Lock()
	sameGeneration := r.desired.Generation == generation
	r.mu.Unlock()
	if !sameGeneration {
		t.Fatal("Close replaced the draining generation before the Execution fence settled")
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

func TestRuntimeCloseFailureRetainsOwnerAndDoesNotRetrySessionStop(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	r := newLifecycleRuntime(t, config)
	var stops atomic.Int32
	r.deps.stopSessions = func(context.Context) error {
		stops.Add(1)
		return errors.New("injected Session join failure")
	}

	for range 2 {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		err := r.Close(ctx)
		cancel()
		if err == nil || !strings.Contains(err.Error(), "injected Session join failure") {
			t.Fatalf("Close = %v, want exact Session join failure", err)
		}
	}
	if got := stops.Load(); got != 1 {
		t.Fatalf("Session stop calls = %d, want one", got)
	}
	progress := mustRuntimeCloseStatus(t, r)
	if progress.Stage != runtimeCloseFailed || !strings.Contains(progress.Failure.Error(), "injected Session join failure") {
		t.Fatalf("Close progress = %+v", progress)
	}
	if owner, err := openTestRuntimeStore(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("failed Session join released environment owner")
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
	r, err := newTestRuntime(t, config.Config, store, runtimeDeps{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = closeTestRuntimeStore(r) })
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
	owner, err := openTestRuntimeStore(config.StateDir)
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
	select {
	case <-r.operationCoordinator.done:
	default:
		t.Fatal("Runtime released owner before joining operation coordinator")
	}
	r.mu.Lock()
	runErr := r.lifecycleRunErr
	r.mu.Unlock()
	if runErr != nil {
		t.Fatalf("graceful Close recorded lifecycle engine failure: %v", runErr)
	}
	owner, err := openTestRuntimeStore(config.StateDir)
	if err != nil {
		t.Fatalf("closed Runtime retained environment owner: %v", err)
	}
	_ = owner.Close()
}

func TestRuntimeUsesOneEnvironmentDesiredResource(t *testing.T) {
	proxy := modelConfigServer(t)
	r := newLifecycleRuntime(t, lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false"))
	runtimeDesired, runtimeFound, err := lifecycle.ReadEnvironmentDesired(context.Background(), r.lifecycleState)
	if err != nil || !runtimeFound {
		t.Fatalf("Runtime desired resource: found=%t err=%v", runtimeFound, err)
	}
	if runtimeDesired.Mode != lifecycle.Running {
		t.Fatalf("Runtime desired=%+v, want Running", runtimeDesired)
	}
	hostDesired, err := r.lifecycleState.List(context.Background(), resource.NewMetadata(lifecycle.Namespace, "HarnessHostDesiredStates.agentplane", "", resource.VersionUndefined))
	if err != nil || len(hostDesired.Items) != 0 {
		t.Fatalf("obsolete Host desired resources: %+v, %v", hostDesired, err)
	}
}

func TestCanceledRuntimeCloseWaiterDoesNotStopNativeCleanup(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	r := newLifecycleRuntime(t, config)
	entered := make(chan struct{})
	release := make(chan struct{})
	defer func() {
		select {
		case <-release:
		default:
			close(release)
		}
	}()
	var once sync.Once
	r.deps.stopSessions = func(ctx context.Context) error {
		once.Do(func() {
			close(entered)
			<-release
		})
		return r.waitSessionStage(ctx, lifecycle.Closed)
	}
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	waitCtx, stopWaiting := context.WithCancel(ctx)
	first := make(chan error, 1)
	go func() { first <- r.Close(waitCtx) }()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("Runtime Close did not reach native cleanup")
	}
	stopWaiting()
	select {
	case err := <-first:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("canceled Close waiter = %v", err)
		}
	case <-ctx.Done():
		t.Fatal("canceled Close waiter remained blocked")
	}
	if owner, err := openTestRuntimeStore(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released owner during native cleanup")
	}
	second := make(chan error, 1)
	go func() { second <- r.Close(ctx) }()
	close(release)
	select {
	case err := <-second:
		if err != nil {
			t.Fatalf("Close after canceled waiter = %v", err)
		}
	case <-ctx.Done():
		t.Fatal("Runtime Close did not finish native cleanup")
	}
	if owner, err := openTestRuntimeStore(config.StateDir); err != nil {
		t.Fatalf("Runtime retained owner after joined cleanup: %v", err)
	} else {
		_ = owner.Close()
	}
}

func TestRuntimeRetainsOwnerWhenSessionCleanupFails(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	r := newLifecycleRuntime(t, config)
	cleanupFailure := errors.New("injected environment owner release failure")
	r.deps.closeSessions = func(ctx context.Context) error {
		r.sessionWorkers.Wait()
		return errors.Join(r.sessions.Close(), cleanupFailure)
	}
	t.Cleanup(func() { _ = closeTestRuntimeStore(r) })
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := r.Close(ctx); !errors.Is(err, cleanupFailure) {
		t.Fatalf("Runtime Close = %v, want Session cleanup failure", err)
	}
	owner, err := openTestRuntimeStore(config.StateDir)
	if err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released environment owner without Session cleanup proof")
	}
}

func TestCloseCannotStartEngineDuringFailedSessionConstruction(t *testing.T) {
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	config.Config.HarnessEnvironment = map[string]string{"CODEX_EXEC_SERVER_URL": "invalid-host-config"}
	entered := make(chan struct{})
	release := make(chan struct{})
	config.deps.beforeSessionConstruction = func() {
		close(entered)
		<-release
	}
	r, err := newRuntime(config.Config, nil, config.deps)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = closeTestRuntimeStore(r) })
	runDone := make(chan error, 1)
	go func() { runDone <- r.Run(context.Background()) }()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("Session construction did not start")
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
		t.Fatal("canceled Close waiter blocked on Session construction")
	}
	select {
	case err := <-closeDone:
		t.Fatalf("Close crossed Session construction barrier: %v", err)
	default:
	}
	close(release)
	select {
	case err := <-runDone:
		if err == nil {
			t.Fatal("Run accepted invalid Session configuration")
		}
	case <-ctx.Done():
		t.Fatal("Run did not return failed Session construction")
	}
	select {
	case err := <-closeDone:
		if err == nil {
			t.Fatal("Close accepted partial Session construction")
		}
	case <-ctx.Done():
		t.Fatal("Close did not return failed Session construction")
	}
	select {
	case <-r.lifecycleDone:
		t.Fatal("Runtime started partially constructed lifecycle engine")
	default:
	}
	owner, err := openTestRuntimeStore(config.StateDir)
	if err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released owner after unsafe Session construction")
	}
}

func TestCloseDuringSuccessfulSessionConstructionJoinsSharedEngine(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.Config.HistoryIngress.Target = startAcknowledgingHistory(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	config.deps.beforeSessionConstruction = func() {
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
		t.Fatal("Session construction did not start")
	}
	expired, cancelExpired := context.WithCancel(ctx)
	cancelExpired()
	if err := r.Close(expired); !errors.Is(err, context.Canceled) {
		t.Fatalf("Close waiter during construction = %v", err)
	}
	close(release)
	if err := r.Close(ctx); err != nil && !errors.Is(err, errSessionComponentLost) {
		t.Fatalf("Close after Session construction = %v", err)
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
	owner, err := openTestRuntimeStore(config.StateDir)
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
	r := newLifecycleRuntime(t, config)
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
	for _, lateHandle := range []bool{false, true} {
		name := "canceled-start"
		if lateHandle {
			name = "late-start-handle"
		}
		t.Run(name, func(t *testing.T) {
			config := executionRuntimeConfig(t, "ws://unused", nil)
			config.expectCloseError = true
			start := config.deps.startExecution
			entered := make(chan struct{})
			release := make(chan struct{})
			var releaseOnce sync.Once
			t.Cleanup(func() { releaseOnce.Do(func() { close(release) }) })
			orphan := newControllerExecutionHandle(nil)
			config.deps.startExecution = func(ctx context.Context, config Config, uid string) (executionHandle, error) {
				if uid == "successor" {
					close(entered)
					<-ctx.Done()
					<-release
					if lateHandle {
						return orphan, nil
					}
					return nil, ctx.Err()
				}
				return start(ctx, config, uid)
			}
			r := newLifecycleRuntime(t, config)
			adoptionDone := make(chan error, 1)
			go func() { adoptionDone <- r.adoptIncarnation(context.Background(), "successor") }()
			select {
			case <-entered:
			case <-time.After(lifecycleTestTimeout):
				t.Fatal("successor Execution start was not admitted")
			}
			closed := make(chan error, 1)
			go func() { closed <- r.Close(context.Background()) }()
			waitRuntime(t, r, r.isClosing, "Close to cancel accepted adoption")
			r.lifecycleCancel()
			select {
			case <-r.operationCoordinator.stopped:
			case <-time.After(lifecycleTestTimeout):
				t.Fatal("Runtime coordinator did not announce exit before joining workers")
			}
			if err := waitClose(t, adoptionDone); err == nil {
				t.Fatal("adoption succeeded after lifecycle engine exit")
			}
			if err := waitClose(t, closed); err == nil {
				t.Fatal("Close succeeded with unsettled Execution startup")
			}
			releaseOnce.Do(func() { close(release) })
			select {
			case <-r.lifecycleDone:
			case <-time.After(lifecycleTestTimeout):
				t.Fatal("Runtime lifecycle engine did not join its transition worker")
			}
			workersDone := make(chan struct{})
			go func() { r.lifecycleWorkers.Wait(); close(workersDone) }()
			select {
			case <-workersDone:
			case <-time.After(lifecycleTestTimeout):
				t.Fatal("accepted Execution start did not settle after engine exit")
			}
			if lateHandle && !orphan.stopRequested() {
				t.Fatal("late Execution start escaped native fencing after engine exit")
			}
			r.mu.Lock()
			unsafe := r.transitionUnsafe
			r.mu.Unlock()
			if unsafe == nil {
				t.Fatal("engine exit did not record the unproved Execution fence")
			}
			if owner, err := openTestRuntimeStore(config.StateDir); err == nil {
				_ = owner.Close()
				t.Fatal("engine exit released environment owner without proving Execution fence")
			}
		})
	}
}

func TestFreshSessionAdmissionInvalidatesIdleToken(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleRuntime(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	before, err := r.facts()
	if err != nil || before.GetIdleSince() == nil {
		t.Fatalf("initial idle facts=%v, %v", before, err)
	}
	if _, err := client.runner.CreateSession(client.callCtx, openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
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
}

func TestSessionComponentFailureStillFencesExecution(t *testing.T) {
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
	r := newLifecycleRuntime(t, config)
	var execution *localExecutionHandle
	select {
	case execution = <-started:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Execution service did not start")
	}
	r.fail(errors.New("simulated Session component failure"))
	closeCtx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := r.Close(closeCtx); err == nil {
		t.Fatal("Runtime silently accepted lost Session lifecycle")
	}
	if !execution.stopRequested() {
		t.Fatal("Runtime skipped Execution fence after Session lifecycle loss")
	}
}

func TestRuntimeCloseRetainsOwnerWhenSessionCleanupUnproved(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	r := newLifecycleRuntime(t, config)
	var stops atomic.Int32
	r.deps.stopSessions = func(context.Context) error {
		stops.Add(1)
		return nil
	}
	r.deps.waitSessionsClosed = func(context.Context) error {
		return errors.New("injected unproved Session native cleanup")
	}
	for range 2 {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		err := r.Close(ctx)
		cancel()
		if err == nil || !strings.Contains(err.Error(), "injected unproved Session native cleanup") {
			t.Fatalf("Close = %v, want native cleanup failure", err)
		}
	}
	if got := stops.Load(); got != 1 {
		t.Fatalf("Session stop calls = %d, want one", got)
	}
	if owner, err := openTestRuntimeStore(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("unproved native cleanup released environment owner")
	}
}

func TestRepeatedQuiesceKeepsCurrentLifecycleGeneration(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleRuntime(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	facts, err := r.facts()
	if err != nil || facts.GetIdleSince() == nil {
		t.Fatalf("initial idle facts=%v, %v", facts, err)
	}
	request := &sessionpb.QuiesceRequest{IfIdleSince: facts.IdleSince}
	if _, err := r.Quiesce(client.ctx, request); err != nil {
		t.Fatal(err)
	}

	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		progress, found, err := lifecycle.ReadStatus(context.Background(), r.lifecycleState)
		return err == nil && found && progress.Generation == r.desired.Generation
	}, "current quiesce lifecycle status")
	r.mu.Lock()
	desired := r.desired
	r.mu.Unlock()
	if _, err := r.Quiesce(client.ctx, request); err != nil {
		t.Fatal(err)
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	if r.desired != desired {
		t.Fatalf("repeated Quiesce changed lifecycle generation: desired=%+v want=%+v", r.desired, desired)
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
	r := newLifecycleRuntime(t, config)
	t.Cleanup(func() { _ = os.WriteFile(bootstrapRelease, nil, 0600); _ = os.WriteFile(setupRelease, nil, 0600) })
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	request := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	request.SetupScript = &setup
	if _, err := client.runner.CreateSession(client.callCtx, request); err != nil {
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
	if owner, err := openTestRuntimeStore(root); err == nil {
		_ = owner.Close()
		t.Fatal("Close released environment ownership while accepted Session setup remained")
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
	restarted := newLifecycleRuntime(t, config)
	successor := startDirectExecutionTestClient(t, restarted, "test-incarnation")
	if _, err := successor.runner.CreateSession(successor.callCtx, request); err != nil {
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

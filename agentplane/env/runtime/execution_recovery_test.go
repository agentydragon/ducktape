package runtime

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

type controllerExecutionHandle struct {
	exited        chan childExit
	stop          func(context.Context) error
	mu            sync.Mutex
	expected      bool
	once          sync.Once
	leaveExitOpen bool
}

func newControllerExecutionHandle(stop func(context.Context) error) *controllerExecutionHandle {
	return &controllerExecutionHandle{exited: make(chan childExit, 1), stop: stop}
}

func (h *controllerExecutionHandle) Executor() executorpb.ExecutorServer {
	return &executorpb.UnimplementedExecutorServer{}
}

func (h *controllerExecutionHandle) StopAndFence(ctx context.Context) error {
	h.mu.Lock()
	h.expected = true
	h.mu.Unlock()
	var err error
	if h.stop != nil {
		err = h.stop(ctx)
	}
	if err == nil && !h.leaveExitOpen {
		h.once.Do(func() { close(h.exited) })
	}
	return err
}

func (h *controllerExecutionHandle) ProcessExit() <-chan childExit { return h.exited }

func (h *controllerExecutionHandle) stopRequested() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.expected
}

func newExecutionControllerRuntime(t *testing.T, start func(context.Context, Config, string) (executionHandle, error)) *Runtime {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	r := &Runtime{
		config:         Config{CodexExecServer: &codexexecserver.Config{URL: "ws://127.0.0.1:1"}},
		intent:         &runtimepb.LifecycleIntent{Mode: runtimepb.LifecycleIntent_RUNNING, IncarnationUid: "incarnation"},
		lifecycleState: store, changes: make(chan struct{}), fatal: make(chan error, 1),
		execCalls: make(map[uint64]context.CancelFunc), execStreams: make(map[uint64]context.CancelFunc),
		deps: runtimeDeps{startExecution: start},
	}
	r.executionLifecycle = &executionLifecycle{r: r, ctx: ctx, state: store}
	if err := r.executionLifecycle.initialize(ctx); err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(&executionController{host: r.executionLifecycle}); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- engine.Run(ctx) }()
	t.Cleanup(func() {
		stopCtx, finish := context.WithTimeout(context.Background(), time.Second)
		defer finish()
		_ = r.stopExecution(stopCtx)
		cancel()
		<-done
		r.lifecycleWorkers.Wait()
	})
	return r
}

func waitExecutionState(t *testing.T, r *Runtime, condition func(executionState) bool) executionState {
	t.Helper()
	deadline := time.NewTimer(3 * time.Second)
	defer deadline.Stop()
	for {
		changed := r.changesSince()
		current, _, err := cosiExecutionState(r)
		if err != nil {
			t.Fatal(err)
		}
		if condition(current) {
			return current
		}
		select {
		case <-changed:
		case <-deadline.C:
			t.Fatalf("Execution lifecycle did not settle: %+v", current)
		}
	}
}

func cosiExecutionState(r *Runtime) (executionState, bool, error) {
	return cosi.Read(context.Background(), r.lifecycleState, executionStateResource(executionState{}))
}

func TestExecutionControllerFencesBeforeReplacementAndDoesNotReplay(t *testing.T) {
	var starts atomic.Int32
	started := make(chan *controllerExecutionHandle, 3)
	fenceEntered := make(chan struct{})
	releaseFence := make(chan struct{})
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		count := starts.Add(1)
		handle := newControllerExecutionHandle(nil)
		if count == 1 {
			handle.stop = func(ctx context.Context) error {
				close(fenceEntered)
				select {
				case <-releaseFence:
					return nil
				case <-ctx.Done():
					return ctx.Err()
				}
			}
		}
		started <- handle
		return handle, nil
	})
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	first := <-started
	first.exited <- childExit{}
	select {
	case <-fenceEntered:
	case <-time.After(time.Second):
		t.Fatal("backend loss did not begin fence")
	}
	current := waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionStopping })
	if current.FencedGeneration == current.Generation || starts.Load() != 1 {
		t.Fatalf("successor before fence proof: state=%+v starts=%d", current, starts.Load())
	}
	r.mu.Lock()
	ready := r.executionOwner.executor("incarnation") != nil || !r.executionOwner.recovering()
	r.mu.Unlock()
	if ready {
		t.Fatal("lost backend remained ready while fence was pending")
	}
	close(releaseFence)
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("fenced loss did not launch successor")
	}
	current = waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionReady && s.Generation == 2 })
	if current.FencedGeneration != 1 {
		t.Fatalf("replacement missing prior fence evidence: %+v", current)
	}
	if starts.Load() != 2 {
		t.Fatalf("unexpected replay or launch: starts=%d", starts.Load())
	}
}

func TestExecutionControllerFailedFenceIsTerminal(t *testing.T) {
	fenceErr := errors.New("executor cgroup still populated")
	var starts atomic.Int32
	started := make(chan *controllerExecutionHandle, 1)
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		starts.Add(1)
		handle := newControllerExecutionHandle(func(context.Context) error { return fenceErr })
		started <- handle
		return handle, nil
	})
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	first := <-started
	first.exited <- childExit{}
	current := waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionFailed })
	input, _, err := cosi.Read(context.Background(), r.lifecycleState, executionInputResource(executionInput{}))
	if err != nil {
		t.Fatal(err)
	}
	if input.Fence.Generation != current.Generation || input.Fence.UID != current.UID || input.Fence.Error != fenceErr.Error() {
		t.Fatalf("failed fence fact lost its generation or cause: %+v", input.Fence)
	}
	if current.FencedGeneration == current.Generation || starts.Load() != 1 {
		t.Fatalf("uncertain fence permitted successor: state=%+v starts=%d", current, starts.Load())
	}
	r.mu.Lock()
	owner := r.executionOwner.handle
	r.mu.Unlock()
	if owner != first {
		t.Fatal("failed fence discarded prior owner")
	}
	if err := r.startExecutionForUID(t.Context(), "next-incarnation"); err == nil {
		t.Fatal("terminal failed fence admitted new incarnation")
	}
}

func TestExecutionControllerImmediateExitAndLatePriorLoss(t *testing.T) {
	var starts atomic.Int32
	var firstFenced atomic.Bool
	handles := make(chan *controllerExecutionHandle, 3)
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		generation := starts.Add(1)
		if generation > 1 && !firstFenced.Load() {
			t.Error("replacement started before the first generation fenced")
		}
		handle := newControllerExecutionHandle(func(context.Context) error {
			if generation == 1 {
				firstFenced.Store(true)
			}
			return nil
		})
		if generation == 1 {
			// The watcher can publish loss before the controller writes Ready.
			handle.exited <- childExit{}
		}
		handles <- handle
		return handle, nil
	})
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	first := <-handles
	second := <-handles
	if first == second {
		t.Fatal("recovery reused the exited owner")
	}
	current := waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionReady && s.Generation == 2 })
	if current.FencedGeneration != 1 || starts.Load() != 2 {
		t.Fatalf("immediate loss did not fence before one replacement: state=%+v starts=%d", current, starts.Load())
	}

	// Model a watcher that observed generation 1's exit but was delayed before
	// publishing it. Its late fact must not replace generation 2's loss fact.
	oldExitObserved := make(chan struct{})
	releaseOldExit := make(chan struct{})
	oldExitPublished := make(chan struct{})
	go func() {
		close(oldExitObserved)
		<-releaseOldExit
		r.executionLifecycle.completeLoss(1, "incarnation", errors.New("late prior exit"))
		close(oldExitPublished)
	}()
	<-oldExitObserved
	second.exited <- childExit{}
	deadline := time.NewTimer(3 * time.Second)
	defer deadline.Stop()
	for {
		input, _, err := cosi.Read(context.Background(), r.lifecycleState, executionInputResource(executionInput{}))
		if err != nil {
			t.Fatal(err)
		}
		if input.Loss.Generation == 2 {
			break
		}
		select {
		case <-r.changesSince():
		case <-deadline.C:
			t.Fatal("second generation loss was not published")
		}
	}
	close(releaseOldExit)
	<-oldExitPublished
	input, _, err := cosi.Read(context.Background(), r.lifecycleState, executionInputResource(executionInput{}))
	if err != nil {
		t.Fatal(err)
	}
	if input.Loss.Generation != 2 || input.Loss.Error != "" {
		t.Fatalf("late prior exit overwrote current loss: %+v", input.Loss)
	}
	third := <-handles
	if third == second {
		t.Fatal("second loss reused its exited owner")
	}
	current = waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionReady && s.Generation == 3 })
	if current.FencedGeneration != 2 || starts.Load() != 3 {
		t.Fatalf("current loss did not fence and recover: state=%+v starts=%d", current, starts.Load())
	}
}

func TestExecutionControllerRetainsRejectedLiveStartHandle(t *testing.T) {
	first := newControllerExecutionHandle(nil)
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		return first, nil
	})
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	orphan := newControllerExecutionHandle(nil)
	r.executionLifecycle.completeStart(1, "incarnation", 1, orphan, t.Context(), nil, false)
	r.executionLifecycle.mu.Lock()
	retained := len(r.executionLifecycle.unfencedStarts) == 1 && r.executionLifecycle.unfencedStarts[0] == orphan
	terminal := r.executionLifecycle.terminal
	r.executionLifecycle.mu.Unlock()
	r.mu.Lock()
	owner := r.executionOwner
	admitted := owner != nil && owner.admitted
	r.mu.Unlock()
	if !retained || terminal == nil || owner == nil || owner.handle != first || admitted {
		t.Fatalf("rejected live handle was not retained with admission closed: retained=%t terminal=%v owner=%v admitted=%t", retained, terminal, owner, admitted)
	}
	if err := r.startExecutionForUID(t.Context(), "another-incarnation"); err == nil {
		t.Fatal("rejected live start admitted another generation")
	}
}

func TestExecutionControllerStopSuppressesRecoveryAndJoinsCanceledWait(t *testing.T) {
	started := make(chan *controllerExecutionHandle, 2)
	fenceEntered := make(chan struct{})
	releaseFence := make(chan struct{})
	var starts atomic.Int32
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		starts.Add(1)
		handle := newControllerExecutionHandle(func(ctx context.Context) error {
			close(fenceEntered)
			select {
			case <-releaseFence:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		})
		started <- handle
		return handle, nil
	})
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	first := <-started
	first.exited <- childExit{}
	select {
	case <-fenceEntered:
	case <-time.After(time.Second):
		t.Fatal("loss did not begin fence")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := r.stopExecution(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled waiter = %v", err)
	}
	close(releaseFence)
	if err := r.stopExecution(t.Context()); err != nil {
		t.Fatalf("accepted stop did not finish: %v", err)
	}
	if starts.Load() != 1 {
		t.Fatalf("quiesce restarted backend %d times", starts.Load())
	}
	current := waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionStopped })
	if current.FencedGeneration != current.Generation {
		t.Fatalf("stop lacked fence evidence: %+v", current)
	}
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
		events <- "start"
		starts <- local
		return local, nil
	}
	r := newLifecycleRuntime(t, config)
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
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.executionOwner != nil && r.executionOwner.handle == replacement && r.executionOwner.executor("test-incarnation") != nil
	}, "replacement Execution API readiness")

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

func TestExecutionControllerLossDuringQuiesceDoesNotRestart(t *testing.T) {
	var starts atomic.Int32
	started := make(chan *controllerExecutionHandle, 2)
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		starts.Add(1)
		handle := newControllerExecutionHandle(nil)
		started <- handle
		return handle, nil
	})
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	first := <-started
	r.mu.Lock()
	r.intent.Mode = runtimepb.LifecycleIntent_QUIESCING
	r.mu.Unlock()
	first.exited <- childExit{}
	current := waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionStopped && s.FencedGeneration == s.Generation })
	if starts.Load() != 1 {
		t.Fatalf("quiescing launched successor: state=%+v starts=%d", current, starts.Load())
	}
	r.mu.Lock()
	r.intent.Mode = runtimepb.LifecycleIntent_RUNNING
	r.mu.Unlock()
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatalf("new epoch could not start after fence: %v", err)
	}
	if starts.Load() != 2 {
		t.Fatalf("new epoch starts=%d, want 2", starts.Load())
	}
}

func TestExecutionControllerCanceledStartupJoinsFenceBeforeNewEpoch(t *testing.T) {
	started := make(chan struct{})
	releaseFence := make(chan struct{})
	var starts atomic.Int32
	r := newExecutionControllerRuntime(t, func(ctx context.Context, _ Config, _ string) (executionHandle, error) {
		if starts.Add(1) == 1 {
			close(started)
			<-ctx.Done()
			<-releaseFence
			return nil, errors.Join(ErrFencedStartup, ctx.Err())
		}
		return newControllerExecutionHandle(nil), nil
	})
	ctx, cancel := context.WithCancel(context.Background())
	waiter := make(chan error, 1)
	go func() { waiter <- r.startExecutionForUID(ctx, "incarnation") }()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("initial startup did not begin")
	}
	cancel()
	if err := <-waiter; !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled startup waiter = %v", err)
	}
	stopDone := make(chan error, 1)
	go func() { stopDone <- r.stopExecution(context.Background()) }()
	select {
	case err := <-stopDone:
		t.Fatalf("stop returned before canceled startup fence: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	if starts.Load() != 1 {
		t.Fatalf("new start before old fence: %d", starts.Load())
	}
	close(releaseFence)
	select {
	case err := <-stopDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("stop did not join canceled startup")
	}
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatalf("successor epoch start: %v", err)
	}
	if starts.Load() != 2 {
		t.Fatalf("successor epoch starts=%d", starts.Load())
	}
}

func TestExecutionControllerFencedStartupErrorReturnsToLiveCaller(t *testing.T) {
	readinessErr := errors.New("Execution API readiness timed out")
	var starts atomic.Int32
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		starts.Add(1)
		return nil, errors.Join(ErrFencedStartup, readinessErr)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := r.startExecutionForUID(ctx, "incarnation"); !errors.Is(err, readinessErr) {
		t.Fatalf("fenced startup error = %v, want readiness cause", err)
	}
	if starts.Load() != 1 {
		t.Fatalf("fenced startup retried under same demand: %d", starts.Load())
	}
	current := waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionStopped && s.AwaitDemand })
	if current.FencedGeneration != current.Generation {
		t.Fatalf("fenced startup lacked proof: %+v", current)
	}
}

func TestExecutionControllerUncertainStartupRetainsOwner(t *testing.T) {
	fenceErr := errors.New("unready process domain not empty")
	var starts atomic.Int32
	handle := newControllerExecutionHandle(func(context.Context) error { return fenceErr })
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		starts.Add(1)
		return handle, fenceErr
	})
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err == nil {
		t.Fatal("uncertain unready startup returned success")
	}
	current := waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionFailed })
	if current.FencedGeneration == current.Generation {
		t.Fatalf("uncertain startup recorded false fence proof: %+v", current)
	}
	r.mu.Lock()
	owner := r.executionOwner.handle
	r.mu.Unlock()
	if owner != handle {
		t.Fatal("uncertain startup discarded owner")
	}
	if err := r.startExecutionForUID(t.Context(), "another-incarnation"); err == nil || starts.Load() != 1 {
		t.Fatalf("uncertain startup admitted successor: err=%v starts=%d", err, starts.Load())
	}
}

func TestExecutionControllerProvedStopJoinsWatcherWithoutExitNotification(t *testing.T) {
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		handle := newControllerExecutionHandle(nil)
		handle.leaveExitOpen = true
		return handle, nil
	})
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	if err := r.stopExecution(t.Context()); err != nil {
		t.Fatal(err)
	}
	joined := make(chan struct{})
	go func() { r.lifecycleWorkers.Wait(); close(joined) }()
	select {
	case <-joined:
	case <-time.After(time.Second):
		t.Fatal("proved stop left ProcessExit watcher blocked")
	}
}

func TestExecutionControllerDoesNotPublishStaleSameUIDStart(t *testing.T) {
	startEntered := make(chan struct{})
	releaseStart := make(chan struct{})
	var starts atomic.Int32
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		if starts.Add(1) == 1 {
			close(startEntered)
			<-releaseStart
		}
		return newControllerExecutionHandle(nil), nil
	})
	ctx, cancel := context.WithCancel(context.Background())
	firstDone := make(chan error, 1)
	go func() { firstDone <- r.startExecutionForUID(ctx, "incarnation") }()
	select {
	case <-startEntered:
	case <-time.After(time.Second):
		t.Fatal("first start was not admitted")
	}
	input, _, err := cosi.Read(context.Background(), r.lifecycleState, executionInputResource(executionInput{}))
	if err != nil {
		t.Fatal(err)
	}
	input.Demand++
	r.executionLifecycle.mu.Lock()
	r.executionLifecycle.startContexts[input.Demand] = t.Context()
	r.executionLifecycle.mu.Unlock()
	if err := cosi.Publish(context.Background(), r.lifecycleState, executionInputResource(input), input, "embedded-execution-host"); err != nil {
		t.Fatal(err)
	}
	close(releaseStart)
	if err := r.executionLifecycle.wait(t.Context(), "incarnation", input.Demand); err != nil {
		t.Fatalf("new epoch result = %v", err)
	}
	cancel()
	if err := <-firstDone; !errors.Is(err, context.Canceled) {
		t.Fatalf("first epoch result = %v", err)
	}
	current := waitExecutionState(t, r, func(s executionState) bool { return s.Phase == executionReady && s.Demand == input.Demand })
	if current.Generation != 2 || current.FencedGeneration != 1 || starts.Load() != 2 {
		t.Fatalf("stale start served a newer epoch: state=%+v starts=%d", current, starts.Load())
	}
}

package runtime

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

type heldSuccessorDemand struct {
	state.State
	armed           atomic.Bool
	failDesiredRead atomic.Bool
	entered         chan struct{}
	release         chan struct{}
	failWatch       <-chan struct{}
	watchOnce       sync.Once
	watchMu         sync.Mutex
	desiredWatch    chan<- []state.Event
	holdAfterCancel bool
}

func (s *heldSuccessorDemand) Get(ctx context.Context, ptr resource.Pointer, opts ...state.GetOption) (resource.Resource, error) {
	if ptr.Type() == lifecycle.EnvironmentDesiredStateType && s.failDesiredRead.Load() {
		return nil, errors.New("injected Close controller desired read failure")
	}
	return s.State.Get(ctx, ptr, opts...)
}

func (s *heldSuccessorDemand) WatchKindAggregated(ctx context.Context, kind resource.Kind, ch chan<- []state.Event, opts ...state.WatchKindOption) error {
	if err := s.State.WatchKindAggregated(ctx, kind, ch, opts...); err != nil {
		return err
	}
	if kind.Type() == lifecycle.EnvironmentDesiredStateType {
		s.watchMu.Lock()
		s.desiredWatch = ch
		s.watchMu.Unlock()
	}
	if s.failWatch != nil {
		s.watchOnce.Do(func() {
			go func() {
				select {
				case <-s.failWatch:
					select {
					case ch <- []state.Event{{Type: state.Errored, Error: errors.New("injected COSI watch failure")}}:
					case <-ctx.Done():
					}
				case <-ctx.Done():
				}
			}()
		})
	}
	return nil
}

func (s *heldSuccessorDemand) failCloseDesiredRead(t *testing.T) {
	t.Helper()
	ptr := resource.NewMetadata(lifecycle.Namespace, lifecycle.EnvironmentDesiredStateType, lifecycle.EnvironmentResourceID, resource.VersionUndefined)
	current, err := s.State.Get(t.Context(), ptr)
	if err != nil {
		t.Fatal(err)
	}
	s.watchMu.Lock()
	ch := s.desiredWatch
	s.watchMu.Unlock()
	if ch == nil {
		t.Fatal("Close controller desired watch was not registered")
	}
	s.failDesiredRead.Store(true)
	select {
	case ch <- []state.Event{{Type: state.Updated, Resource: current}}:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("failed to wake Close controller")
	}
}

func (s *heldSuccessorDemand) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if err := s.State.Modify(ctx, value, update, opts...); err != nil {
		return err
	}
	if value.Metadata().Type() != session.ReconcileSnapshotType || !s.armed.Load() {
		return nil
	}
	current, found, err := lifecycle.ReadEnvironmentDesired(ctx, s.State)
	if err != nil {
		return err
	}
	if found && current.IncarnationUID == "successor" && current.Mode == lifecycle.Running && s.armed.CompareAndSwap(true, false) {
		close(s.entered)
		if s.holdAfterCancel {
			<-s.release
			return nil
		}
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func TestRuntimeCloseWaitsForRunningPreparationAndExecutionFence(t *testing.T) {
	store := &heldSuccessorDemand{State: state.WrapCore(namespaced.NewState(inmem.Build)), entered: make(chan struct{}), release: make(chan struct{})}
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
	config.CodexExecServer = &codexexecserver.Config{URL: "ws://unused"}
	config.CodexBinary = "/bin/false"
	config.HarnessEnvironment = map[string]string{}
	successor := &heldCloseExecution{started: make(chan struct{}), release: make(chan struct{})}
	config.deps.startExecution = func(_ context.Context, _ Config, uid string) (executionHandle, error) {
		if uid == "successor" {
			return successor, nil
		}
		return newControllerExecutionHandle(nil), nil
	}
	r := newLifecycleRunnerWithState(t, config, store)
	var releaseDemand, releaseFence sync.Once
	t.Cleanup(func() {
		releaseDemand.Do(func() { close(store.release) })
		releaseFence.Do(func() { close(successor.release) })
	})
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	if _, err := client.runner.CreateSession(client.callCtx, openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	store.armed.Store(true)
	adoptionDone := make(chan error, 1)
	go func() { adoptionDone <- r.adoptIncarnation(context.Background(), "successor") }()
	select {
	case <-store.entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("successor Running revision did not reach Session demand reset")
	}
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, func() bool { return r.isClosing() }, "Close to revoke admission while Running preparation settles")
	r.mu.Lock()
	preparing, accepted := r.desired, r.admissionOpen
	r.mu.Unlock()
	if preparing.Mode != lifecycle.Running || preparing.IncarnationUID != "successor" || accepted {
		t.Fatalf("revision while Close waits for preparation: desired=%+v accepted=%t", preparing, accepted)
	}
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned before the Running preparation settled: %v", err)
	default:
	}
	if successor.fences.Load() != 0 {
		t.Fatal("Execution fence started before the Session component completed native shutdown")
	}
	if owner, err := openTestRuntimeStore(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("environment owner released while Running preparation was unsettled")
	}
	releaseDemand.Do(func() { close(store.release) })
	select {
	case <-successor.started:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("successor Execution fence did not start")
	}
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned before Execution fence completed: %v", err)
	default:
	}
	if owner, err := openTestRuntimeStore(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("environment owner released before Execution fence")
	}
	releaseFence.Do(func() { close(successor.release) })
	if err := waitClose(t, closeDone); err != nil {
		t.Fatalf("Close after preparation and fence settled: %v", err)
	}
	if err := waitClose(t, adoptionDone); err == nil {
		t.Fatal("superseded adoption succeeded during Close")
	}
	if got := successor.fences.Load(); got != 1 {
		t.Fatalf("successor Execution fence attempts = %d, want 1", got)
	}
	owner, err := openTestRuntimeStore(config.StateDir)
	if err != nil {
		t.Fatalf("environment owner retained after successful Close: %v", err)
	}
	if err := owner.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestEngineWatchFailureWakesBlockedAdoptionBeforeWorkerJoin(t *testing.T) {
	failWatch := make(chan struct{})
	store := &heldSuccessorDemand{State: state.WrapCore(namespaced.NewState(inmem.Build)), entered: make(chan struct{}), release: make(chan struct{}), failWatch: failWatch, holdAfterCancel: true}
	workspace := t.TempDir()
	config := lifecycleConfig(t, modelConfigServer(t).URL, workspace, nil, "/bin/false")
	config.CodexExecServer = &codexexecserver.Config{URL: "ws://unused"}
	config.CodexBinary = "/bin/false"
	config.HarnessEnvironment = map[string]string{}
	config.expectCloseError = true
	config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
		return newControllerExecutionHandle(nil), nil
	}
	r := newLifecycleRunnerWithState(t, config, store)
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	if _, err := client.runner.CreateSession(client.callCtx, openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	var release sync.Once
	t.Cleanup(func() { release.Do(func() { close(store.release) }) })
	store.armed.Store(true)
	adoptionDone := make(chan error, 1)
	go func() { adoptionDone <- r.adoptIncarnation(context.Background(), "successor") }()
	select {
	case <-store.entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("successor Running preparation did not block")
	}
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, r.isClosing, "Close to revoke admission")
	close(failWatch)
	select {
	case <-r.operationCoordinator.stopped:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("engine watch failure did not stop operation owner")
	}
	if err := waitClose(t, adoptionDone); err == nil {
		t.Fatal("adoption succeeded after engine watch failure")
	}
	if err := waitClose(t, closeDone); err == nil {
		t.Fatal("Close released ownership after engine watch failure")
	}
	select {
	case <-r.lifecycleDone:
		t.Fatal("engine joined before the accepted preparation worker settled")
	default:
	}
	release.Do(func() { close(store.release) })
	select {
	case <-r.lifecycleDone:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("engine did not join operation owner after blocked worker settled")
	}
	r.mu.Lock()
	runErr := r.lifecycleRunErr
	r.mu.Unlock()
	if runErr == nil || !strings.Contains(runErr.Error(), "injected COSI watch failure") {
		t.Fatalf("engine failure = %v, want injected watch failure", runErr)
	}
}

func TestCloseControllerFailureStopsBlockedOperationWithoutRuntimeRun(t *testing.T) {
	store := &heldSuccessorDemand{State: state.WrapCore(namespaced.NewState(inmem.Build)), entered: make(chan struct{}), release: make(chan struct{}), holdAfterCancel: true}
	workspace := t.TempDir()
	config := lifecycleConfig(t, modelConfigServer(t).URL, workspace, nil, "/bin/false")
	config.CodexExecServer = &codexexecserver.Config{URL: "ws://unused"}
	config.CodexBinary = "/bin/false"
	config.HarnessEnvironment = map[string]string{}
	config.expectCloseError = true
	config.deps.startExecution = func(context.Context, Config, string) (executionHandle, error) {
		return newControllerExecutionHandle(nil), nil
	}
	r := newLifecycleRunnerWithState(t, config, store)
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	if _, err := client.runner.CreateSession(client.callCtx, openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	var release sync.Once
	t.Cleanup(func() { release.Do(func() { close(store.release) }) })
	store.armed.Store(true)
	adoptionDone := make(chan error, 1)
	go func() { adoptionDone <- r.adoptIncarnation(context.Background(), "successor") }()
	select {
	case <-store.entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("successor Running preparation did not block")
	}
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, r.isClosing, "Close to revoke admission")
	store.failCloseDesiredRead(t)
	select {
	case <-r.lifecycleCoordinator.stopped:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Close controller did not exit after desired read failure")
	}
	select {
	case <-r.operationCoordinator.stopped:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Close controller exit did not stop operation owner")
	}
	if err := waitClose(t, adoptionDone); err == nil {
		t.Fatal("adoption succeeded after Close controller failure")
	}
	if err := waitClose(t, closeDone); err == nil {
		t.Fatal("Close released ownership after Close controller failure")
	}
	select {
	case <-r.lifecycleDone:
		t.Fatal("engine joined before blocked preparation settled")
	default:
	}
	release.Do(func() { close(store.release) })
	select {
	case <-r.lifecycleDone:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("engine did not join operation owner after Close controller failure")
	}
	r.mu.Lock()
	controllerErr := r.lifecycleControllerErr
	r.mu.Unlock()
	if controllerErr == nil || !strings.Contains(controllerErr.Error(), "injected Close controller desired read failure") {
		t.Fatalf("Close controller error = %v, want injected desired read failure", controllerErr)
	}
}

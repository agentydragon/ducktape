package codexexecserver

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

func TestControllerConsumesNonzeroDemandFromCOSIAndStartsExecutor(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	var workers sync.WaitGroup
	var starts, probes atomic.Int32
	var instance *supervision.Instance
	var stopOnce sync.Once
	done := make(chan error, 1)
	instance = &supervision.Instance{
		PID:  42,
		Done: done,
		Stop: func(context.Context) error {
			stopOnce.Do(func() { done <- nil })
			return nil
		},
	}
	processes := supervision.NewHost(ctx, store, lifecycle.Namespace, &workers, func(err error) {
		t.Errorf("process host error: %v", err)
	})
	admission := func(ctx context.Context, expected lifecycle.Desired, effect func(context.Context) error) error {
		current, found, err := lifecycle.ReadDesired(ctx, store)
		if err != nil || !found || current != expected || current.Mode != lifecycle.Running {
			return err
		}
		return effect(ctx)
	}
	host, err := NewHost(Config{URL: "ws://127.0.0.1:4501"}, Dependencies{
		Context: ctx, State: store, Workers: &workers, Processes: processes,
		Admission: admission, OnError: func(err error) { t.Errorf("exec-server host error: %v", err) },
	}, Adapters{
		Start: func(context.Context) (*supervision.Instance, error) {
			starts.Add(1)
			return instance, nil
		},
		Probe: func(context.Context) error {
			probes.Add(1)
			return nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := host.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	if err := lifecycle.PublishDesired(ctx, store, lifecycle.Desired{Mode: lifecycle.Running, Generation: 1}); err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(&testAggregateStatusController{}); err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(processes.Controller()); err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(host.Controller()); err != nil {
		t.Fatal(err)
	}
	runDone := make(chan error, 1)
	go func() { runDone <- engine.Run(ctx) }()
	if err := host.RequestLocked(ctx); err != nil {
		t.Fatal(err)
	}
	waitRuntime(t, ctx, host, func(current Runtime) bool { return current.Phase == Ready }, "executor readiness")

	input, found, err := ReadInput(ctx, store)
	if err != nil || !found || input.Demand == 0 {
		t.Fatalf("persisted executor input = %+v, found=%t err=%v; want nonzero demand", input, found, err)
	}
	if starts.Load() != 1 || probes.Load() == 0 {
		t.Fatalf("start/probe counts = %d/%d, want one start and at least one probe", starts.Load(), probes.Load())
	}

	if err := lifecycle.PublishDesired(ctx, store, lifecycle.Desired{Mode: lifecycle.Quiescing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	waitRuntime(t, ctx, host, func(current Runtime) bool { return current.Phase == Stopped && current.ProcessID == "" }, "executor fencing")
	cancel()
	select {
	case err := <-runDone:
		if err != nil && !errors.Is(err, context.Canceled) {
			t.Fatalf("controller runtime: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("controller runtime did not stop")
	}
	workers.Wait()
}

type testAggregateStatusController struct{}

func (*testAggregateStatusController) Name() string { return "execserver-test-aggregate" }

func (*testAggregateStatusController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs:      []controller.Input{{Namespace: lifecycle.Namespace, Type: lifecycle.DesiredStateType, Kind: controller.InputQPrimary}},
		Outputs:     []controller.Output{{Type: lifecycle.AggregateStatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*testAggregateStatusController) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() != lifecycle.DesiredStateType {
		return nil, nil
	}
	return []resource.Pointer{resource.NewMetadata(lifecycle.Namespace, lifecycle.AggregateStatusType, lifecycle.ResourceID, resource.VersionUndefined)}, nil
}

func (*testAggregateStatusController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	desired, found, err := lifecycle.ReadDesired(ctx, r)
	if err != nil || !found {
		return err
	}
	stage := lifecycle.StageRunning
	if desired.Mode != lifecycle.Running {
		stage = lifecycle.Fencing
	}
	_, err = lifecycle.WriteStatus(ctx, r, lifecycle.Status{Generation: desired.Generation, Stage: stage})
	return err
}

func TestProbeCompletionIsPublishedBeforeConsumption(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	blocked := &blockedOperationState{State: store, publishing: make(chan struct{}), release: make(chan struct{})}
	var workers sync.WaitGroup
	processes := supervision.NewHost(ctx, blocked, lifecycle.Namespace, &workers, func(err error) { t.Errorf("process host error: %v", err) })
	host, err := NewHost(Config{URL: "ws://127.0.0.1:4501"}, Dependencies{
		Context: ctx, State: blocked, Workers: &workers, Processes: processes,
		Admission: func(context.Context, lifecycle.Desired, func(context.Context) error) error { return nil },
		OnError:   func(err error) { t.Errorf("exec-server host error: %v", err) },
	}, Adapters{
		Start: func(context.Context) (*supervision.Instance, error) { return nil, errors.New("unused") },
		Probe: func(context.Context) error { return nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	host.startProbe(func(context.Context) error { return nil })
	op := host.probeOp
	var releaseOnce sync.Once
	release := func() { releaseOnce.Do(func() { close(blocked.release) }) }
	defer release()
	<-blocked.publishing
	if op == nil || op.Finished() {
		t.Fatal("probe became consumable before its completion was published")
	}
	release()
	workers.Wait()
	result, done := host.takeProbe()
	if !done || result == nil || result.Err() != nil || host.probePending() {
		t.Fatalf("consumed probe = %+v, done=%t pending=%t", result, done, host.probePending())
	}
	if _, found, err := readOperation(ctx, blocked, result.ID()); err != nil || found {
		t.Fatalf("probe operation resource survived consumption: found=%t err=%v", found, err)
	}
}

func TestProbeCompletionPublicationFailureIsRetainedAndReportedOutsideLock(t *testing.T) {
	ctx := context.Background()
	publicationErr := errors.New("completion state write failed")
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	failed := &failedOperationState{State: store, err: publicationErr}
	var workers sync.WaitGroup
	processes := supervision.NewHost(ctx, failed, lifecycle.Namespace, &workers, func(err error) {
		t.Errorf("process host error: %v", err)
	})
	reported := make(chan error, 1)
	var host *Host
	var reportHeldHostLock atomic.Bool
	var err error
	host, err = NewHost(Config{URL: "ws://127.0.0.1:4501"}, Dependencies{
		Context: ctx, State: failed, Workers: &workers, Processes: processes,
		Admission: func(context.Context, lifecycle.Desired, func(context.Context) error) error { return nil },
		OnError: func(err error) {
			if !host.mu.TryLock() {
				reportHeldHostLock.Store(true)
			} else {
				host.mu.Unlock()
			}
			reported <- err
		},
	}, Adapters{
		Start: func(context.Context) (*supervision.Instance, error) { return nil, errors.New("unused") },
		Probe: func(context.Context) error { return nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	host.startProbe(func(context.Context) error { return nil })
	workers.Wait()
	result, done := host.takeProbe()
	if !done || result == nil || host.probePending() {
		t.Fatalf("consumed probe = %+v, done=%t pending=%t", result, done, host.probePending())
	}
	if !errors.Is(result.Err(), publicationErr) {
		t.Errorf("probe error = %v, want completion publication error", result.Err())
	}
	if err := <-reported; !errors.Is(err, publicationErr) {
		t.Errorf("reported error = %v, want completion publication error", err)
	}
	if reportHeldHostLock.Load() {
		t.Error("completion publication failure was reported while holding the host mutex")
	}
}

func TestControllerSelfProgressesPastReadinessDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		store := state.WrapCore(namespaced.NewState(inmem.Build))
		engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
		if err != nil {
			t.Fatal(err)
		}
		var workers sync.WaitGroup
		var starts, stops atomic.Int32
		var stopOnce sync.Once
		done := make(chan error, 1)
		instance := &supervision.Instance{
			PID:  43,
			Done: done,
			Stop: func(context.Context) error {
				stops.Add(1)
				stopOnce.Do(func() { done <- nil })
				return nil
			},
		}
		processes := supervision.NewHost(ctx, store, lifecycle.Namespace, &workers, func(err error) {
			t.Errorf("process host error: %v", err)
		})
		admission := func(ctx context.Context, expected lifecycle.Desired, effect func(context.Context) error) error {
			current, found, err := lifecycle.ReadDesired(ctx, store)
			if err != nil || !found || current != expected || current.Mode != lifecycle.Running {
				return err
			}
			return effect(ctx)
		}
		host, err := NewHost(Config{URL: "ws://127.0.0.1:4501"}, Dependencies{
			Context: ctx, State: store, Workers: &workers, Processes: processes,
			Admission: admission, OnError: func(err error) { t.Errorf("exec-server host error: %v", err) },
		}, Adapters{
			Start: func(context.Context) (*supervision.Instance, error) {
				starts.Add(1)
				return instance, nil
			},
			Probe: func(ctx context.Context) error {
				<-ctx.Done()
				return ctx.Err()
			},
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := host.Initialize(ctx); err != nil {
			t.Fatal(err)
		}
		if err := lifecycle.PublishDesired(ctx, store, lifecycle.Desired{Mode: lifecycle.Running, Generation: 1}); err != nil {
			t.Fatal(err)
		}
		if err := engine.RegisterQController(&testAggregateStatusController{}); err != nil {
			t.Fatal(err)
		}
		if err := engine.RegisterQController(processes.Controller()); err != nil {
			t.Fatal(err)
		}
		if err := engine.RegisterQController(host.Controller()); err != nil {
			t.Fatal(err)
		}
		runDone := make(chan error, 1)
		go func() { runDone <- engine.Run(ctx) }()
		defer func() {
			cancel()
			if err := <-runDone; err != nil && !errors.Is(err, context.Canceled) {
				t.Errorf("controller runtime: %v", err)
			}
			workers.Wait()
		}()

		synctest.Wait()
		if err := host.RequestLocked(ctx); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		initial, found, err := host.RuntimeState(ctx)
		if err != nil || !found || initial.Phase != Probing {
			t.Fatalf("executor before readiness deadline = %+v, found=%t err=%v", initial, found, err)
		}
		time.Sleep(readyBudget)
		synctest.Wait()
		current, found, err := host.RuntimeState(ctx)
		if err != nil || !found {
			t.Fatalf("readiness timeout runtime found=%t err=%v", found, err)
		}
		if current.Phase != Retrying || current.ProcessID != "" || current.RestartFailures != 1 {
			t.Fatalf("readiness timeout runtime = %+v, want retrying after self-scheduled stop", current)
		}
		if !strings.Contains(current.Error, "did not become ready within") {
			t.Fatalf("readiness timeout error = %q", current.Error)
		}
		if starts.Load() != 1 || stops.Load() != 1 {
			t.Fatalf("readiness start/stop counts = %d/%d, want one fenced executor", starts.Load(), stops.Load())
		}
	})
}

func TestControllerHealthToleratesFailuresResetsAndFencesBeforeSuccessor(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		store := state.WrapCore(namespaced.NewState(inmem.Build))
		engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
		if err != nil {
			t.Fatal(err)
		}
		var workers sync.WaitGroup
		var starts, stops, probes atomic.Int32
		processes := supervision.NewHost(ctx, store, lifecycle.Namespace, &workers, func(err error) {
			t.Errorf("process host error: %v", err)
		})
		admission := func(ctx context.Context, expected lifecycle.Desired, effect func(context.Context) error) error {
			current, found, err := lifecycle.ReadDesired(ctx, store)
			if err != nil || !found || current != expected || current.Mode != lifecycle.Running {
				return err
			}
			return effect(ctx)
		}
		host, err := NewHost(Config{URL: "ws://127.0.0.1:4501"}, Dependencies{
			Context: ctx, State: store, Workers: &workers, Processes: processes,
			Admission: admission, OnError: func(err error) { t.Errorf("exec-server host error: %v", err) },
		}, Adapters{
			Start: func(context.Context) (*supervision.Instance, error) {
				starts.Add(1)
				done := make(chan error, 1)
				var stopOnce sync.Once
				return &supervision.Instance{
					PID:  44,
					Done: done,
					Stop: func(context.Context) error {
						stops.Add(1)
						stopOnce.Do(func() { done <- nil })
						return nil
					},
				}, nil
			},
			Probe: func(context.Context) error {
				switch probes.Add(1) {
				case 2, 3, 5, 6, 7:
					return errors.New("temporary dispatcher timeout")
				default:
					return nil
				}
			},
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := host.Initialize(ctx); err != nil {
			t.Fatal(err)
		}
		if err := lifecycle.PublishDesired(ctx, store, lifecycle.Desired{Mode: lifecycle.Running, Generation: 1}); err != nil {
			t.Fatal(err)
		}
		if err := engine.RegisterQController(&testAggregateStatusController{}); err != nil {
			t.Fatal(err)
		}
		if err := engine.RegisterQController(processes.Controller()); err != nil {
			t.Fatal(err)
		}
		if err := engine.RegisterQController(host.Controller()); err != nil {
			t.Fatal(err)
		}
		runDone := make(chan error, 1)
		go func() { runDone <- engine.Run(ctx) }()
		defer func() {
			cancel()
			if err := <-runDone; err != nil && !errors.Is(err, context.Canceled) {
				t.Errorf("controller runtime: %v", err)
			}
			workers.Wait()
		}()
		if err := host.RequestLocked(ctx); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		current, found, err := host.RuntimeState(ctx)
		if err != nil || !found || current.Phase != Ready {
			t.Fatalf("executor before health probes = %+v, found=%t err=%v", current, found, err)
		}

		for index, failures := range []uint8{1, 2, 0} {
			time.Sleep(healthInterval)
			synctest.Wait()
			current, found, err = host.RuntimeState(ctx)
			if err != nil || !found || current.Phase != Ready || current.ConsecutiveProbeFailures != failures || probes.Load() < int32(index+2) {
				t.Fatalf("executor after transient health sequence %d = %+v, found=%t err=%v", index, current, found, err)
			}
		}

		for _, failures := range []uint8{1, 2, 3} {
			time.Sleep(healthInterval)
			synctest.Wait()
			current, found, err = host.RuntimeState(ctx)
			if err != nil || !found {
				t.Fatalf("executor during persistent health failures found=%t err=%v", found, err)
			}
			if failures < 3 {
				if current.Phase != Ready || current.ConsecutiveProbeFailures != failures {
					t.Fatalf("executor after %d persistent failures = %+v", failures, current)
				}
				continue
			}
			if current.Phase != Retrying || current.ProcessID != "" || stops.Load() != 1 {
				t.Fatalf("executor after third health failure = %+v stops=%d, want retry after fencing", current, stops.Load())
			}
		}
		if starts.Load() != 1 {
			t.Fatalf("started a successor before retry delay: starts=%d stops=%d", starts.Load(), stops.Load())
		}

		time.Sleep(restartDelay(1))
		synctest.Wait()
		current, found, err = host.RuntimeState(ctx)
		if err != nil || !found || current.Generation != 2 || current.Phase != Ready || starts.Load() != 2 || stops.Load() != 1 {
			t.Fatalf("executor successor after completed fence = %+v starts=%d stops=%d found=%t err=%v", current, starts.Load(), stops.Load(), found, err)
		}
	})
}

type blockedOperationState struct {
	state.State
	publishing chan struct{}
	release    chan struct{}
}

type failedOperationState struct {
	state.State
	err error
}

func (s *failedOperationState) Modify(
	ctx context.Context,
	value resource.Resource,
	update func(resource.Resource) error,
	opts ...state.UpdateOption,
) error {
	if result, ok := value.(*typed.Resource[operationResult, componentExtension[operationResult]]); ok && result.TypedSpec().Done {
		return s.err
	}
	return s.State.Modify(ctx, value, update, opts...)
}

func (s *blockedOperationState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if result, ok := value.(*typed.Resource[operationResult, componentExtension[operationResult]]); ok && result.TypedSpec().Done {
		close(s.publishing)
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return s.State.Modify(ctx, value, update, opts...)
}

func waitRuntime(t *testing.T, ctx context.Context, host *Host, predicate func(Runtime) bool, want string) Runtime {
	t.Helper()
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		current, _, err := host.RuntimeState(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if predicate(current) {
			return current
		}
		select {
		case <-ticker.C:
		case <-deadline.C:
			t.Fatalf("timed out waiting for %s: runtime=%+v", want, current)
		case <-ctx.Done():
			t.Fatalf("context canceled while waiting for %s: %v", want, ctx.Err())
		}
	}
}

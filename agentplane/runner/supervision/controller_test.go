package supervision

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const (
	processTestRequestType                    = "ProcessTestRequests.agentplane"
	processTestRequester                      = "process-test-requester"
	processTestNamespace   resource.Namespace = "process-test"
	processTestTimeout                        = 10 * time.Second
)

type processTestRequestSpec struct {
	Active        bool
	Generation    uint64
	Revision      uint64
	StartDeadline time.Time
}

func (processTestRequestSpec) resourceType() resource.Type            { return processTestRequestType }
func (value processTestRequestSpec) DeepCopy() processTestRequestSpec { return value }

type processTestRequesterController struct {
	host       *Host
	launchMu   sync.RWMutex
	launches   map[string]Launch
	reconciles atomic.Int64
	teardownOK atomic.Bool
}

func (*processTestRequesterController) Name() string { return processTestRequester }

func (*processTestRequesterController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: processTestNamespace, Type: processTestRequestType, Kind: controller.InputQPrimary},
			{Namespace: processTestNamespace, Type: DesiredStateType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: DesiredStateType, Kind: controller.OutputShared}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*processTestRequesterController) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	switch ptr.Type() {
	case processTestRequestType:
		return []resource.Pointer{resource.NewMetadata(processTestNamespace, processTestRequestType, ptr.ID(), resource.VersionUndefined)}, nil
	case DesiredStateType:
		return []resource.Pointer{resource.NewMetadata(processTestNamespace, processTestRequestType, ptr.ID(), resource.VersionUndefined)}, nil
	default:
		return nil, nil
	}
}

func (c *processTestRequesterController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	c.reconciles.Add(1)
	request, found, err := readResource[processTestRequestSpec](ctx, r, processTestNamespace, ptr.ID())
	if err != nil || !found {
		return err
	}
	processPointer := resource.NewMetadata(processTestNamespace, DesiredStateType, ptr.ID(), resource.VersionUndefined)
	if request.Active {
		c.launchMu.RLock()
		launch := c.launches[ptr.ID()]
		c.launchMu.RUnlock()
		if launch == nil {
			return fmt.Errorf("no process launch configured for %q", ptr.ID())
		}
		return c.host.Request(ctx, r, ptr.ID(), DesiredState{
			Owner: processTestRequester, Generation: request.Generation,
			StartDeadline: request.StartDeadline, StopTimeout: time.Minute,
		}, launch)
	}

	ready, err := r.Teardown(ctx, processPointer)
	if state.IsNotFoundError(err) {
		return nil
	}
	if err != nil {
		return err
	}
	c.teardownOK.Store(ready)
	if !ready {
		return nil
	}
	return r.Destroy(ctx, processPointer)
}

func (c *processTestRequesterController) setLaunch(id string, launch Launch) {
	c.launchMu.Lock()
	defer c.launchMu.Unlock()
	c.launches[id] = launch
}

type processControllerTestRuntime struct {
	ctx       context.Context
	state     state.State
	host      *Host
	requester *processTestRequesterController
	done      chan struct{}
}

func newProcessControllerTestRuntime(t *testing.T) *processControllerTestRuntime {
	t.Helper()

	ctx, cancel := context.WithCancel(context.Background())
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := runtime.NewRuntime(store, zap.L().Named("process-controller-test"))
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	workers := &sync.WaitGroup{}
	fatal := make(chan error, 1)
	reportFatal := func(err error) {
		if err == nil {
			return
		}
		select {
		case fatal <- err:
		default:
		}
	}
	host := NewHost(ctx, store, processTestNamespace, workers, reportFatal)
	if err := engine.RegisterQController(host.Controller()); err != nil {
		cancel()
		t.Fatal(err)
	}
	requester := &processTestRequesterController{host: host, launches: make(map[string]Launch)}
	if err := engine.RegisterQController(requester); err != nil {
		cancel()
		t.Fatal(err)
	}

	f := &processControllerTestRuntime{ctx: ctx, state: store, host: host, requester: requester, done: make(chan struct{})}
	go func() {
		defer close(f.done)
		if err := engine.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
			reportFatal(err)
		}
	}()
	t.Cleanup(func() {
		cancel()
		<-f.done
		workers.Wait()
		select {
		case err := <-fatal:
			t.Errorf("process controller test runtime failed: %v", err)
		default:
		}
	})
	return f
}

func (f *processControllerTestRuntime) publishRequest(t *testing.T, id string, request processTestRequestSpec) {
	t.Helper()
	ctx, cancel := context.WithTimeout(f.ctx, processTestTimeout)
	defer cancel()
	if request.StartDeadline.IsZero() {
		current, found, err := readResource[processTestRequestSpec](ctx, f.state, processTestNamespace, id)
		if err != nil {
			t.Fatal(err)
		}
		if found {
			request.StartDeadline = current.StartDeadline
		} else {
			request.StartDeadline = time.Now().Add(time.Hour)
		}
	}
	err := safe.StateModify(ctx, f.state, resourceFor(processTestNamespace, id, request), func(r *typed.Resource[processTestRequestSpec, resourceExtension[processTestRequestSpec]]) error {
		*r.TypedSpec() = request
		return nil
	}, state.WithUpdateOwner("runner-host"))
	if err != nil {
		t.Fatal(err)
	}
}

func (f *processControllerTestRuntime) status(t *testing.T, id string) (Status, bool) {
	t.Helper()
	current, found, err := ReadStatus(f.ctx, f.state, processTestNamespace, id)
	if err != nil {
		t.Fatal(err)
	}
	return current, found
}

func (f *processControllerTestRuntime) desiredExists(t *testing.T, id string) bool {
	t.Helper()
	_, found, err := ReadDesiredState(f.ctx, f.state, processTestNamespace, id)
	if err != nil {
		t.Fatal(err)
	}
	return found
}

func waitProcessStatus(t *testing.T, f *processControllerTestRuntime, id, want string, predicate func(Status) bool) Status {
	t.Helper()
	var current Status
	var found bool
	for range 20 {
		synctest.Wait()
		current, found = f.status(t, id)
		if found && predicate(current) {
			return current
		}
	}
	t.Fatalf("process %q did not reach %s: found=%t status=%+v", id, want, found, current)
	return Status{}
}

func waitProcessRequestReconciles(t *testing.T, f *processControllerTestRuntime, count int64) {
	t.Helper()
	for range 20 {
		synctest.Wait()
		if f.requester.reconciles.Load() >= count {
			return
		}
	}
	t.Fatalf("requester reconciles = %d, want at least %d", f.requester.reconciles.Load(), count)
}

func TestProcessControllerStartsOnceAndRetainsTerminalStatus(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newProcessControllerTestRuntime(t)
		const id = "once"
		done := make(chan error, 1)
		var starts atomic.Int32
		f.requester.setLaunch(id, func(context.Context) (*Instance, error) {
			starts.Add(1)
			return &Instance{Done: done, PID: 123, Stop: func(context.Context) error { return nil }}, nil
		})

		f.publishRequest(t, id, processTestRequestSpec{Active: true, Generation: 1, Revision: 1})
		started := waitProcessStatus(t, f, id, "running", func(current Status) bool {
			return current.Phase == Running
		})
		if !started.Started || started.PID != 123 {
			t.Fatalf("started status = %+v, want launch facts retained", started)
		}

		f.publishRequest(t, id, processTestRequestSpec{Active: true, Generation: 1, Revision: 2})
		waitProcessRequestReconciles(t, f, 3)
		synctest.Wait()
		if got := starts.Load(); got != 1 {
			t.Fatalf("launch count after duplicate request/requeue = %d, want 1", got)
		}

		done <- nil
		close(done)
		exited := waitProcessStatus(t, f, id, "exited", func(current Status) bool {
			return current.Phase == Exited
		})
		if !exited.Started || exited.PID != 123 {
			t.Errorf("terminal status = %+v, want original process facts", exited)
		}

		f.publishRequest(t, id, processTestRequestSpec{Active: true, Generation: 1, Revision: 3})
		waitProcessRequestReconciles(t, f, 4)
		synctest.Wait()
		retained, found := f.status(t, id)
		if !found || retained.Phase != Exited {
			t.Fatalf("status after repeated terminal reconcile = %+v found=%t, want retained Exited", retained, found)
		}
		if got := starts.Load(); got != 1 {
			t.Errorf("launch count after terminal requeue = %d, want 1", got)
		}

		f.publishRequest(t, id, processTestRequestSpec{Active: false, Generation: 1, Revision: 4})
		synctest.Wait()
	})
}

func TestProcessTeardownWaitsForLateStartAndStopBarrier(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newProcessControllerTestRuntime(t)
		const id = "late-start"
		startEntered, releaseStart := make(chan struct{}), make(chan struct{})
		stopEntered, releaseStop := make(chan struct{}), make(chan struct{})
		var releaseStartOnce, releaseStopOnce sync.Once
		release := func() {
			releaseStartOnce.Do(func() { close(releaseStart) })
			releaseStopOnce.Do(func() { close(releaseStop) })
		}
		t.Cleanup(release)
		var starts, stops atomic.Int32
		var launchCtx context.Context
		f.requester.setLaunch(id, func(ctx context.Context) (*Instance, error) {
			starts.Add(1)
			launchCtx = ctx
			close(startEntered)
			<-releaseStart // Model a native launch that replies after cancellation.
			return &Instance{Done: make(chan error), PID: 456, Stop: func(context.Context) error {
				stops.Add(1)
				close(stopEntered)
				<-releaseStop
				return nil
			}}, nil
		})

		f.publishRequest(t, id, processTestRequestSpec{Active: true, Generation: 1, Revision: 1})
		for range 20 {
			synctest.Wait()
			select {
			case <-startEntered:
				goto startEnteredLoop
			default:
			}
		}
		t.Fatal("process launch callback did not start")
	startEnteredLoop:
		f.publishRequest(t, id, processTestRequestSpec{Active: false, Generation: 1, Revision: 2})
		synctest.Wait()
		select {
		case <-launchCtx.Done():
		default:
			t.Fatal("teardown did not cancel the in-flight launch")
		}
		if f.requester.teardownOK.Load() {
			t.Fatal("owner teardown completed while the launch callback was still unresolved")
		}
		if !f.desiredExists(t, id) {
			t.Fatal("desired state was destroyed before the late launch could be fenced")
		}

		releaseStartOnce.Do(func() { close(releaseStart) })
		for range 20 {
			synctest.Wait()
			select {
			case <-stopEntered:
				goto stopEnteredLoop
			default:
			}
		}
		t.Fatal("late process instance did not enter its stop barrier")
	stopEnteredLoop:
		stopping := waitProcessStatus(t, f, id, "stopping", func(current Status) bool {
			return current.Phase == Stopping && current.PID == 456
		})
		if stopping.StopError != "" || f.requester.teardownOK.Load() {
			t.Fatalf("process crossed teardown barrier early: status=%+v teardownReady=%t", stopping, f.requester.teardownOK.Load())
		}
		if !f.desiredExists(t, id) {
			t.Fatal("desired state was destroyed before stop completed")
		}

		releaseStopOnce.Do(func() { close(releaseStop) })
		for range 20 {
			synctest.Wait()
			if !f.desiredExists(t, id) {
				break
			}
		}
		if f.desiredExists(t, id) {
			t.Fatal("desired state remained after start returned and stop succeeded")
		}
		if _, found := f.status(t, id); found {
			t.Fatal("terminal status remained after owner destroyed the process")
		}
		if _, registered := f.host.snapshot(id); registered {
			t.Fatal("process host retained observers or handles after destroy")
		}
		if got := starts.Load(); got != 1 {
			t.Errorf("start count = %d, want 1", got)
		}
		if got := stops.Load(); got != 1 {
			t.Errorf("stop count = %d, want 1", got)
		}
	})
}

func TestProcessFailedFenceRetainsFinalizerAndStopError(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newProcessControllerTestRuntime(t)
		const id = "failed-fence"
		done := make(chan error)
		stopFailure := errors.New("native process would not stop")
		var starts, stops atomic.Int32
		f.requester.setLaunch(id, func(context.Context) (*Instance, error) {
			starts.Add(1)
			return &Instance{Done: done, PID: 789, Stop: func(context.Context) error {
				stops.Add(1)
				return stopFailure
			}}, nil
		})

		f.publishRequest(t, id, processTestRequestSpec{Active: true, Generation: 1, Revision: 1})
		waitProcessStatus(t, f, id, "running", func(current Status) bool { return current.Phase == Running })
		f.publishRequest(t, id, processTestRequestSpec{Active: false, Generation: 1, Revision: 2})
		failed := waitProcessStatus(t, f, id, "failed stop", func(current Status) bool {
			return current.Phase == Stopping && current.StopError != ""
		})
		if !strings.Contains(failed.StopError, stopFailure.Error()) {
			t.Fatalf("StopError = %q, want %q", failed.StopError, stopFailure)
		}
		if f.requester.teardownOK.Load() {
			t.Fatal("owner teardown completed despite failed fencing")
		}
		if !f.desiredExists(t, id) {
			t.Fatal("desired state was destroyed despite failed fencing")
		}

		// Another owner input change forces reconciliation while teardown is still pending.
		f.publishRequest(t, id, processTestRequestSpec{Active: false, Generation: 1, Revision: 3})
		waitProcessRequestReconciles(t, f, 4)
		input, found, err := readResource[inputSpec](f.ctx, f.state, processTestNamespace, id)
		if err != nil || !found {
			t.Fatalf("read supervision input to force another reconcile: found=%t err=%v", found, err)
		}
		input.Revision++
		if err := publishResource(f.ctx, f.state, processTestNamespace, id, input); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		retained, found := f.status(t, id)
		if !found || retained.Phase != Stopping || retained.StopError == "" {
			t.Fatalf("status after further reconcile = %+v found=%t, want failed stop retained", retained, found)
		}
		if got := starts.Load(); got != 1 {
			t.Errorf("start count after failed fence = %d, want 1", got)
		}
		if got := stops.Load(); got != 1 {
			t.Errorf("stop count after failed fence = %d, want 1", got)
		}
	})
}

func TestProcessNaturalExitFencesBeforeTerminalAndIsolatesSuccessor(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newProcessControllerTestRuntime(t)
		oldDone := make(chan error, 1)
		oldStopEntered, releaseOldStop := make(chan struct{}), make(chan struct{})
		newDone := make(chan error)
		var oldStopOnce, newDoneOnce sync.Once
		t.Cleanup(func() {
			oldStopOnce.Do(func() { close(releaseOldStop) })
			newDoneOnce.Do(func() { close(newDone) })
		})
		var oldStarts, newStarts atomic.Int32
		f.requester.setLaunch("old", func(context.Context) (*Instance, error) {
			oldStarts.Add(1)
			return &Instance{Done: oldDone, PID: 1001, Stop: func(context.Context) error {
				close(oldStopEntered)
				<-releaseOldStop
				return nil
			}}, nil
		})
		f.requester.setLaunch("new", func(context.Context) (*Instance, error) {
			newStarts.Add(1)
			return &Instance{Done: newDone, PID: 1002, Stop: func(context.Context) error {
				newDoneOnce.Do(func() { close(newDone) })
				return nil
			}}, nil
		})

		f.publishRequest(t, "old", processTestRequestSpec{Active: true, Generation: 1, Revision: 1})
		waitProcessStatus(t, f, "old", "running", func(current Status) bool { return current.Phase == Running })
		oldDone <- errors.New("unexpected process exit")
		close(oldDone)
		for range 20 {
			synctest.Wait()
			select {
			case <-oldStopEntered:
				goto oldStopLoop
			default:
			}
		}
		t.Fatal("natural exit did not enter cleanup")
	oldStopLoop:
		oldStopping := waitProcessStatus(t, f, "old", "fencing", func(current Status) bool {
			return current.Phase == Stopping
		})
		if oldStopping.Phase == Exited {
			t.Fatal("natural exit became terminal before cleanup completed")
		}

		f.publishRequest(t, "new", processTestRequestSpec{Active: true, Generation: 1, Revision: 1})
		waitProcessStatus(t, f, "new", "running successor", func(current Status) bool { return current.Phase == Running })
		if got := oldStarts.Load(); got != 1 {
			t.Errorf("old process start count = %d, want 1", got)
		}
		if got := newStarts.Load(); got != 1 {
			t.Errorf("successor start count = %d, want 1", got)
		}

		oldStopOnce.Do(func() { close(releaseOldStop) })
		oldExited := waitProcessStatus(t, f, "old", "exited after fence", func(current Status) bool { return current.Phase == Exited })
		if !strings.Contains(oldExited.ExitError, "unexpected process exit") {
			t.Errorf("old ExitError = %q, want natural exit reason", oldExited.ExitError)
		}
		successor, found := f.status(t, "new")
		if !found || successor.Phase != Running || successor.PID != 1002 {
			t.Fatalf("successor status after old cleanup = %+v found=%t", successor, found)
		}
		f.publishRequest(t, "old", processTestRequestSpec{Active: false, Generation: 1, Revision: 2})
		synctest.Wait()
		if _, registered := f.host.snapshot("old"); registered {
			t.Fatal("old process host entry remained after its owner destroyed it")
		}
		if current, ok := f.status(t, "new"); !ok || current.Phase != Running || current.PID != 1002 {
			t.Fatalf("old process cleanup affected successor: status=%+v found=%t", current, ok)
		}
		f.publishRequest(t, "new", processTestRequestSpec{Active: false, Generation: 1, Revision: 2})
		synctest.Wait()
	})
}

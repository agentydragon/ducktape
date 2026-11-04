package supervision

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

const processTestNamespace resource.Namespace = "process-test"

func TestSharedProcessControllerKeepsOwnerLifetimesSeparate(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		store := state.WrapCore(namespaced.NewState(inmem.Build))
		engine, err := runtime.NewRuntime(store, zap.L().Named("shared-process-test"))
		if err != nil {
			t.Fatal(err)
		}
		var runtimeWorkers, hostWorkers sync.WaitGroup
		runtimeHost := NewHost(ctx, store, processTestNamespace, RuntimeDomain, &runtimeWorkers, func(err error) { t.Errorf("Runtime process: %v", err) })
		harnessCtx, cancelHarness := context.WithCancel(ctx)
		defer cancelHarness()
		harnessHost := runtimeHost.Scope(harnessCtx, HarnessDomain, &hostWorkers, func(err error) { t.Errorf("Host process: %v", err) })
		if err := engine.RegisterQController(runtimeHost.Controller()); err != nil {
			t.Fatal(err)
		}
		engineDone := make(chan error, 1)
		go func() { engineDone <- engine.Run(ctx) }()
		defer func() {
			cancel()
			if err := <-engineDone; err != nil {
				t.Errorf("shared engine: %v", err)
			}
			runtimeWorkers.Wait()
			hostWorkers.Wait()
		}()
		for _, pair := range []struct {
			host *Host
			id   string
		}{{runtimeHost, "runtime"}, {harnessHost, "harness"}} {
			// Stop proves the process-domain fence without relying on the exit
			// observer winning its race against cancellation during joining.
			if err := pair.host.requestNative(ctx, pair.id, DesiredState{Owner: pair.id, StopTimeout: time.Second}, func(context.Context) (*Instance, error) {
				return &Instance{Done: make(chan error), Stop: func(context.Context) error { return nil }}, nil
			}); err != nil {
				t.Fatal(err)
			}
		}
		for _, pair := range []struct {
			id     string
			domain Domain
		}{{"runtime", RuntimeDomain}, {"harness", HarnessDomain}} {
			var current Status
			for range 20 {
				synctest.Wait()
				current, _, err = ReadStatus(ctx, store, processTestNamespace, pair.id)
				if err != nil {
					t.Fatal(err)
				}
				if current.Phase == Running {
					break
				}
			}
			if current.Phase != Running || current.Owner != pair.id {
				t.Fatalf("%s status = %+v", pair.id, current)
			}
			desired, found, err := ReadDesiredState(ctx, store, processTestNamespace, pair.id)
			if err != nil || !found || desired.Domain != pair.domain {
				t.Fatalf("%s desired = %+v found=%t err=%v", pair.id, desired, found, err)
			}
			res, err := store.Get(ctx, resourceFor(processTestNamespace, pair.id, DesiredState{}).Metadata())
			if err != nil || !res.Metadata().Finalizers().Has(runtimeHost.Controller().Name()) {
				t.Fatalf("%s finalizer missing: desired=%v err=%v", pair.id, res, err)
			}
		}
		if err := harnessHost.requestNative(ctx, "runtime", DesiredState{Owner: "runtime", StopTimeout: time.Second}, func(context.Context) (*Instance, error) {
			t.Error("another scope launched the same attempt")
			return nil, nil
		}); err == nil {
			t.Fatal("another scope registered an existing attempt")
		}
		if _, err := store.Teardown(ctx, resourceFor(processTestNamespace, "harness", DesiredState{}).Metadata(), state.WithTeardownOwner(hostOwner)); err != nil {
			t.Fatal(err)
		}
		var hostStatus Status
		for range 20 {
			synctest.Wait()
			hostStatus, _, err = ReadStatus(ctx, store, processTestNamespace, "harness")
			if err != nil {
				t.Fatal(err)
			}
			if hostStatus.Phase == Exited {
				break
			}
		}
		if hostStatus.Phase != Exited {
			t.Fatalf("Host process did not finish fencing: %+v", hostStatus)
		}
		cancelHarness()
		hostWorkers.Wait()
		status, _, err := ReadStatus(ctx, store, processTestNamespace, "runtime")
		if err != nil || status.Phase != Running {
			t.Fatalf("Runtime process after Host stop = %+v, %v", status, err)
		}
		if _, err := store.Teardown(ctx, resourceFor(processTestNamespace, "runtime", DesiredState{}).Metadata(), state.WithTeardownOwner(hostOwner)); err != nil {
			t.Fatal(err)
		}
	})
}

type processControllerTestRuntime struct {
	ctx   context.Context
	state state.State
	host  *Host
	done  chan struct{}
	calls sync.WaitGroup
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
	host := NewHost(ctx, store, processTestNamespace, RuntimeDomain, workers, reportFatal)
	if err := engine.RegisterQController(host.Controller()); err != nil {
		cancel()
		t.Fatal(err)
	}
	f := &processControllerTestRuntime{ctx: ctx, state: store, host: host, done: make(chan struct{})}
	go func() {
		defer close(f.done)
		if err := engine.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
			reportFatal(err)
		}
	}()
	t.Cleanup(func() {
		cancel()
		<-f.done
		f.calls.Wait()
		workers.Wait()
		select {
		case err := <-fatal:
			t.Errorf("process controller test runtime failed: %v", err)
		default:
		}
	})
	return f
}

func (f *processControllerTestRuntime) request(t *testing.T, id string, launch Launch) {
	t.Helper()
	if err := f.host.requestNative(f.ctx, id, DesiredState{
		Owner: "process-test-owner", Generation: 1, StopTimeout: time.Minute,
	}, launch); err != nil {
		t.Fatal(err)
	}
}

func (f *processControllerTestRuntime) stop(id string) <-chan error {
	done := make(chan error, 1)
	p := &ManagedProcess{host: f.host, id: id}
	f.calls.Add(1)
	go func() {
		defer f.calls.Done()
		done <- p.Stop(f.ctx)
	}()
	return done
}

func (f *processControllerTestRuntime) requeue(t *testing.T, id string) {
	t.Helper()
	facts, found, err := readResource[observationSpec](f.ctx, f.state, processTestNamespace, id)
	if err != nil || !found {
		t.Fatalf("read process observations: found=%t err=%v", found, err)
	}
	facts.Revision++
	if err := publishResource(f.ctx, f.state, processTestNamespace, id, facts); err != nil {
		t.Fatal(err)
	}
	synctest.Wait()
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

func TestProcessControllerStartsOnceAndRetainsTerminalStatus(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newProcessControllerTestRuntime(t)
		const id = "once"
		done := make(chan error, 1)
		var starts atomic.Int32
		f.request(t, id, func(context.Context) (*Instance, error) {
			starts.Add(1)
			return &Instance{Done: done, PID: 123, Stop: func(context.Context) error { return nil }}, nil
		})

		started := waitProcessStatus(t, f, id, "running", func(current Status) bool {
			return current.Phase == Running
		})
		if !started.Started || started.PID != 123 {
			t.Fatalf("started status = %+v, want launch facts retained", started)
		}

		desired, found, err := ReadDesiredState(f.ctx, f.state, processTestNamespace, id)
		if err != nil || !found {
			t.Fatalf("read attempt intent: found=%t err=%v", found, err)
		}
		changed := desired
		changed.Generation++
		replacement := func(context.Context) (*Instance, error) {
			t.Error("duplicate registration replaced the original launch")
			return nil, errors.New("unexpected replacement launch")
		}
		if err := f.host.requestNative(f.ctx, id, changed, replacement); err == nil {
			t.Fatal("accepted a changed attempt generation")
		}

		f.requeue(t, id)
		if got := starts.Load(); got != 1 {
			t.Fatalf("launch count after duplicate observation/requeue = %d, want 1", got)
		}

		done <- nil
		close(done)
		exited := waitProcessStatus(t, f, id, "exited", func(current Status) bool {
			return current.Phase == Exited
		})
		if !exited.Started || exited.PID != 123 {
			t.Errorf("terminal status = %+v, want original process facts", exited)
		}

		f.requeue(t, id)
		retained, found := f.status(t, id)
		if !found || retained.Phase != Exited {
			t.Fatalf("status after repeated terminal reconcile = %+v found=%t, want retained Exited", retained, found)
		}
		if got := starts.Load(); got != 1 {
			t.Errorf("launch count after terminal requeue = %d, want 1", got)
		}

		if err := <-f.stop(id); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
	})
}

func TestProcessControllerRetainsSystemdObservationsAcrossFence(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newProcessControllerTestRuntime(t)
		const id = "systemd-attempt"
		done := make(chan error, 1)
		stopEntered := make(chan struct{})
		releaseStop := make(chan struct{})
		var release sync.Once
		t.Cleanup(func() { release.Do(func() { close(releaseStop) }) })
		observers := make(chan func(SystemdObservation), 1)
		var starts atomic.Int32
		f.request(t, id, func(context.Context) (*Instance, error) {
			starts.Add(1)
			observe := func(observation SystemdObservation) { f.host.observeSystemd(id, observation) }
			observers <- observe
			observe(SystemdObservation{Revision: 1, Unit: "old.service", Snapshot: SystemdUnitSnapshot{InvocationID: "old", ActiveState: "active"}})
			return &Instance{
				Done: done, PID: 123,
				Stop: func(context.Context) error {
					close(stopEntered)
					<-releaseStop
					return nil
				},
			}, nil
		})
		initial := waitProcessStatus(t, f, id, "initial systemd observation", func(status Status) bool {
			return status.Phase == Running && status.Systemd.Revision == 1
		})
		if initial.Systemd.Unit != "old.service" || initial.Systemd.Snapshot.InvocationID != "old" {
			t.Fatalf("initial observation = %+v", initial.Systemd)
		}
		observe := <-observers
		observe(SystemdObservation{Revision: 3, Unit: "old.service", Snapshot: SystemdUnitSnapshot{InvocationID: "old", ActiveState: "failed"}, Error: "manager disconnected"})
		observe(SystemdObservation{Revision: 2, Unit: "old.service", Snapshot: SystemdUnitSnapshot{InvocationID: "old", ActiveState: "active"}})
		facts, found, err := readResource[observationSpec](f.ctx, f.state, processTestNamespace, id)
		if err != nil || !found || facts.Systemd.Revision != 3 || facts.Systemd.Snapshot.ActiveState != "failed" {
			t.Fatalf("process observations lost newest systemd observation: facts=%+v found=%t err=%v", facts, found, err)
		}
		current := waitProcessStatus(t, f, id, "newest systemd observation", func(status Status) bool {
			return status.Systemd.Revision == 3
		})
		if current.Systemd.Error != "manager disconnected" || current.Systemd.Snapshot.ActiveState != "failed" {
			t.Fatalf("newest observation = %+v", current.Systemd)
		}
		observe(SystemdObservation{Revision: 4, Unit: "old.service", ExitState: SystemdExitObserved, ExitCode: 23})
		waitProcessStatus(t, f, id, "exit observation before cleanup", func(status Status) bool {
			return status.Systemd.ExitState == SystemdExitObserved && status.Systemd.ExitCode == 23
		})
		done <- nil
		close(done)
		stopping := waitProcessStatus(t, f, id, "fence pending", func(status Status) bool { return status.Phase == Stopping })
		<-stopEntered
		if stopping.Systemd.Revision != 4 || stopping.Systemd.ExitCode != 23 {
			t.Fatalf("observation lost before fence: %+v", stopping)
		}
		desired, err := f.state.Get(f.ctx, resourceFor(processTestNamespace, id, DesiredState{}).Metadata())
		if err != nil || !desired.Metadata().Finalizers().Has(f.host.Controller().Name()) {
			t.Fatalf("fence must retain finalizer: desired=%v err=%v", desired, err)
		}
		release.Do(func() { close(releaseStop) })
		exited := waitProcessStatus(t, f, id, "fenced", func(status Status) bool { return status.Phase == Exited })
		if exited.Systemd.Revision != 4 || starts.Load() != 1 {
			t.Fatalf("terminal status=%+v starts=%d", exited, starts.Load())
		}
		if err := <-f.stop(id); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		observe(SystemdObservation{Revision: 5, Unit: "stale.service"})
		synctest.Wait()
		if _, found := f.status(t, id); found {
			t.Fatal("stale observation recreated retired process status")
		}
		if _, found, err := readResource[observationSpec](f.ctx, f.state, processTestNamespace, id); err != nil || found {
			t.Fatalf("stale observation recreated process observations: found=%t err=%v", found, err)
		}
	})
}

func TestProcessControllerPublishesSystemdEvidenceWhileStartIsBlocked(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newProcessControllerTestRuntime(t)
		const id = "uncertain-systemd-start"
		release := make(chan struct{})
		var released sync.Once
		t.Cleanup(func() { released.Do(func() { close(release) }) })
		var starts atomic.Int32
		f.request(t, id, func(context.Context) (*Instance, error) {
			starts.Add(1)
			f.host.observeSystemd(id, SystemdObservation{Revision: 1, Unit: "uncertain.service", Error: "manager disconnected"})
			<-release
			return nil, errors.New("fenced start failure")
		})
		starting := waitProcessStatus(t, f, id, "blocked systemd start", func(status Status) bool {
			return status.Phase == Starting && status.Systemd.Revision == 1
		})
		if starting.Systemd.Unit != "uncertain.service" || starting.Systemd.Error != "manager disconnected" || starts.Load() != 1 {
			t.Fatalf("starting status=%+v starts=%d", starting, starts.Load())
		}
		desired, err := f.state.Get(f.ctx, resourceFor(processTestNamespace, id, DesiredState{}).Metadata())
		if err != nil || !desired.Metadata().Finalizers().Has(f.host.Controller().Name()) {
			t.Fatalf("unresolved start must retain finalizer: desired=%v err=%v", desired, err)
		}
		released.Do(func() { close(release) })
		exited := waitProcessStatus(t, f, id, "fenced start failure", func(status Status) bool { return status.Phase == Exited })
		if exited.StartError != "fenced start failure" || starts.Load() != 1 {
			t.Fatalf("terminal status=%+v starts=%d", exited, starts.Load())
		}
	})
}

// teardownPhaseRace reproduces a second caller moving the desired resource
// to teardown between COSI's initial read and its phase-checked update.
type teardownPhaseRace struct {
	state.State
	once          sync.Once
	phaseConflict atomic.Bool
}

func (s *teardownPhaseRace) Teardown(ctx context.Context, ptr resource.Pointer, opts ...state.TeardownOption) (bool, error) {
	var injected bool
	var err error
	s.once.Do(func() {
		injected = true
		_, err = s.State.Teardown(ctx, ptr, opts...)
		if err == nil {
			_, err = s.State.UpdateWithConflicts(ctx, ptr, func(resource.Resource) error { return nil }, state.WithUpdateOwner(hostOwner))
			s.phaseConflict.Store(state.IsPhaseConflictError(err))
		}
	})
	if injected {
		return false, err
	}
	return s.State.Teardown(ctx, ptr, opts...)
}

func TestConcurrentTeardownStillWaitsForProcessFence(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newProcessControllerTestRuntime(t)
		race := &teardownPhaseRace{State: f.state}
		f.host.state = race
		const id = "concurrent-teardown"
		stopEntered, releaseStop := make(chan struct{}), make(chan struct{})
		var release sync.Once
		t.Cleanup(func() { release.Do(func() { close(releaseStop) }) })
		f.request(t, id, func(context.Context) (*Instance, error) {
			return &Instance{Done: make(chan error), Stop: func(context.Context) error {
				close(stopEntered)
				<-releaseStop
				return nil
			}}, nil
		})
		waitProcessStatus(t, f, id, "running", func(status Status) bool { return status.Phase == Running })
		stopDone := f.stop(id)
		synctest.Wait()
		if !race.phaseConflict.Load() {
			t.Fatal("test did not reproduce a teardown phase conflict")
		}
		select {
		case <-stopEntered:
		default:
			t.Fatal("teardown did not start the process fence")
		}
		select {
		case err := <-stopDone:
			t.Fatalf("stop returned before the process fence: %v", err)
		default:
		}
		release.Do(func() { close(releaseStop) })
		if err := <-stopDone; err != nil {
			t.Fatalf("stop after concurrent teardown: %v", err)
		}
		if f.desiredExists(t, id) {
			t.Fatal("desired state remained after the process fence")
		}
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
		f.request(t, id, func(ctx context.Context) (*Instance, error) {
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
		stopDone := f.stop(id)
		synctest.Wait()
		select {
		case <-launchCtx.Done():
		default:
			t.Fatal("teardown did not cancel the in-flight launch")
		}
		select {
		case err := <-stopDone:
			t.Fatalf("stop completed while launch was unresolved: %v", err)
		default:
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
		if stopping.StopError != "" {
			t.Fatalf("process stop failed: %+v", stopping)
		}
		select {
		case err := <-stopDone:
			t.Fatalf("stop completed before fence was released: %v", err)
		default:
		}
		if !f.desiredExists(t, id) {
			t.Fatal("desired state was destroyed before stop completed")
		}

		releaseStopOnce.Do(func() { close(releaseStop) })
		if err := <-stopDone; err != nil {
			t.Fatal(err)
		}
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
		f.request(t, id, func(context.Context) (*Instance, error) {
			starts.Add(1)
			return &Instance{Done: done, PID: 789, Stop: func(context.Context) error {
				stops.Add(1)
				return stopFailure
			}}, nil
		})

		waitProcessStatus(t, f, id, "running", func(current Status) bool { return current.Phase == Running })
		stopDone := f.stop(id)
		failed := waitProcessStatus(t, f, id, "failed stop", func(current Status) bool {
			return current.Phase == Stopping && current.StopError != ""
		})
		if !strings.Contains(failed.StopError, stopFailure.Error()) {
			t.Fatalf("StopError = %q, want %q", failed.StopError, stopFailure)
		}
		if err := <-stopDone; err == nil {
			t.Fatal("stop succeeded despite failed fencing")
		}
		desired, err := f.state.Get(f.ctx, resourceFor(processTestNamespace, id, DesiredState{}).Metadata())
		if err != nil || !desired.Metadata().Finalizers().Has(f.host.Controller().Name()) {
			t.Fatalf("failed fence must retain finalizer: desired=%v err=%v", desired, err)
		}
		if !f.desiredExists(t, id) {
			t.Fatal("desired state was destroyed despite failed fencing")
		}

		f.requeue(t, id)
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
		f.request(t, "old", func(context.Context) (*Instance, error) {
			oldStarts.Add(1)
			return &Instance{Done: oldDone, PID: 1001, Stop: func(context.Context) error {
				close(oldStopEntered)
				<-releaseOldStop
				return nil
			}}, nil
		})

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

		f.request(t, "new", func(context.Context) (*Instance, error) {
			newStarts.Add(1)
			return &Instance{Done: newDone, PID: 1002, Stop: func(context.Context) error {
				newDoneOnce.Do(func() { close(newDone) })
				return nil
			}}, nil
		})
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
		if err := <-f.stop("old"); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		if _, registered := f.host.snapshot("old"); registered {
			t.Fatal("old process host entry remained after its owner destroyed it")
		}
		if current, ok := f.status(t, "new"); !ok || current.Phase != Running || current.PID != 1002 {
			t.Fatalf("old process cleanup affected successor: status=%+v found=%t", current, ok)
		}
		if err := <-f.stop("new"); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
	})
}

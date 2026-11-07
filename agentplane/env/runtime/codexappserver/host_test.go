package codexappserver

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

	"github.com/agentydragon/ducktape/agentplane/env/oneoff"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
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

type fixture struct {
	ctx     context.Context
	cancel  context.CancelFunc
	store   state.State
	host    *Host
	workers sync.WaitGroup
	runDone chan error
}

type operationSignalState struct {
	state.State
	failRevision  uint64
	err           error
	blockRevision uint64
	blockAt       *atomic.Uint64
	publishing    chan struct{}
	release       chan struct{}
	once          sync.Once
}

func (s *operationSignalState) UpdateWithConflicts(ctx context.Context, ptr resource.Pointer, update state.UpdaterFunc, opts ...state.UpdateOption) (resource.Resource, error) {
	return s.State.UpdateWithConflicts(ctx, ptr, func(value resource.Resource) error {
		if err := update(value); err != nil {
			return err
		}
		signal, ok := value.(*typed.Resource[LifecycleSignal, lifecycle.Extension[LifecycleSignal]])
		if !ok {
			return nil
		}
		revision := signal.TypedSpec().OperationRevision
		if s.err != nil && revision == s.failRevision {
			return s.err
		}
		blockRevision := s.blockRevision
		if s.blockAt != nil {
			blockRevision = s.blockAt.Load()
		}
		if revision == blockRevision {
			s.once.Do(func() {
				close(s.publishing)
				select {
				case <-s.release:
				case <-ctx.Done():
				}
			})
		}
		return nil
	}, opts...)
}

func operationHost(t *testing.T, store state.State, report func(error)) *Host {
	t.Helper()
	host := &Host{deps: Dependencies{Context: t.Context(), State: store, Workers: &sync.WaitGroup{}, OnError: report}, changed: make(chan struct{})}
	if err := host.Initialize(t.Context()); err != nil {
		t.Fatal(err)
	}
	return host
}

func TestInitialOperationPublicationFailureClearsSlotAndSkipsWork(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	publicationErr := errors.New("initial operation state write failed")
	workers := &sync.WaitGroup{}
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	reported := make(chan error, 1)
	host := &Host{
		deps: Dependencies{
			Context: ctx, State: &operationSignalState{State: store, failRevision: 1, err: publicationErr}, Workers: workers,
			OnError: func(err error) { reported <- err },
		},
		changed: make(chan struct{}),
	}
	if err := host.Initialize(ctx); err != nil {
		t.Fatal(err)
	}

	var effects atomic.Int32
	err := host.startOperation(&host.initializing, func(context.Context) error {
		effects.Add(1)
		return nil
	})
	workers.Wait()
	if !errors.Is(err, publicationErr) {
		t.Fatalf("start error = %v, want initial publication failure", err)
	}
	if got := effects.Load(); got != 0 {
		t.Fatalf("app-server effects = %d, want 0", got)
	}
	if host.initializing != nil {
		t.Fatal("operation slot remained owned after initial publication failure")
	}
	select {
	case err := <-reported:
		if !errors.Is(err, publicationErr) {
			t.Fatalf("reported error = %v, want initial publication failure", err)
		}
	default:
		t.Fatal("initial publication failure was not reported")
	}
}

// waitSubscriptionContext stops Host after its readiness predicate has run and
// before it enters the notification select. This makes the missed-wake window
// observable without changing production code.
type waitSubscriptionContext struct {
	context.Context
	parked  chan struct{}
	release chan struct{}
	once    sync.Once
}

func (c *waitSubscriptionContext) Done() <-chan struct{} {
	c.once.Do(func() {
		close(c.parked)
		<-c.release
	})
	return c.Context.Done()
}

func newFixture(t *testing.T) *fixture {
	return newFixtureWithOnChange(t, nil)
}

func newFixtureWithOnChange(t *testing.T, onChange func()) *fixture {
	return newFixtureWithConfiguration(t, onChange, nil)
}

func newFixtureWithConfiguration(t *testing.T, onChange func(), configure func(*Host)) *fixture {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	f := &fixture{ctx: ctx, cancel: cancel, store: state.WrapCore(namespaced.NewState(inmem.Build)), runDone: make(chan error, 1)}
	engine, err := runtime.NewRuntime(f.store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	processes := supervision.NewHost(ctx, f.store, lifecycle.Namespace, supervision.HarnessDomain, &f.workers, func(err error) { t.Errorf("process host: %v", err) })
	admit := func(ctx context.Context, expected lifecycle.EnvironmentDesired, effect func(context.Context) error) error {
		current, found, err := lifecycle.ReadEnvironmentDesired(ctx, f.store)
		if err != nil || !found || current != expected || current.Mode != lifecycle.Running {
			return err
		}
		return effect(ctx)
	}
	binary := filepath.Join(t.TempDir(), "codex-app-server")
	script := "#!/bin/sh\nIFS= read -r request\nprintf '%s\\n' '{\"id\":\"agentplane-initialize\",\"result\":{}}'\nprintf '%s\\n' '{\"method\":\"test/early\"}'\ncat >/dev/null\n"
	if err := os.WriteFile(binary, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	supervisor, err := supervision.NewPID1Supervisor()
	if err != nil {
		t.Fatal(err)
	}
	f.host, err = NewHost(harness.LaunchConfig{Binary: binary, BaseURL: "https://codex.example.invalid/v1", Token: "test"}, Dependencies{
		Context: ctx, State: f.store, Workers: &f.workers, Processes: processes,
		Supervisor: supervisor, StateDir: t.TempDir(),
		RequestAdmission: func(ctx context.Context, effect func(context.Context) error) error {
			current, found, err := lifecycle.ReadEnvironmentDesired(ctx, f.store)
			if err != nil {
				return err
			}
			if !found || current.Mode != lifecycle.Running {
				return errors.New("not accepting")
			}
			return effect(ctx)
		},
		Admission: admit, OnError: func(err error) { t.Errorf("app-server host: %v", err) }, OnChange: onChange,
	})
	if err != nil {
		t.Fatal(err)
	}
	if configure != nil {
		configure(f.host)
	}
	if err := f.host.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	if err := lifecycle.PublishEnvironmentDesired(ctx, f.store, lifecycle.EnvironmentDesired{Mode: lifecycle.Running, Generation: 1}); err != nil {
		t.Fatal(err)
	}
	for _, c := range []controller.QController{&aggregateController{}, processes.Controller(), f.host.Controller()} {
		if err := engine.RegisterQController(c); err != nil {
			t.Fatal(err)
		}
	}
	go func() { f.runDone <- engine.Run(ctx) }()
	t.Cleanup(func() {
		f.cancel()
		select {
		case err := <-f.runDone:
			if err != nil && !errors.Is(err, context.Canceled) {
				t.Errorf("component COSI runtime: %v", err)
			}
		case <-time.After(10 * time.Second):
			t.Error("component COSI runtime did not stop")
		}
		f.workers.Wait()
	})
	return f
}

type aggregateController struct{}

func (*aggregateController) Name() string { return "codexappserver-test-aggregate" }
func (*aggregateController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs:      []controller.Input{{Namespace: lifecycle.Namespace, Type: lifecycle.EnvironmentDesiredStateType, Kind: controller.InputQPrimary}},
		Outputs:     []controller.Output{{Type: lifecycle.AggregateStatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*aggregateController) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, _ controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	return []resource.Pointer{resource.NewMetadata(lifecycle.Namespace, lifecycle.EnvironmentDesiredStateType, lifecycle.ResourceID, resource.VersionUndefined)}, nil
}

func (*aggregateController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	desired, found, err := lifecycle.ReadEnvironmentDesired(ctx, r)
	if err != nil || !found {
		return err
	}
	stage := lifecycle.StageRunning
	if desired.Mode == lifecycle.Quiescing {
		stage = lifecycle.Draining
	} else if desired.Mode != lifecycle.Running {
		stage = lifecycle.Stopping
	}
	_, err = lifecycle.WriteStatus(ctx, r, lifecycle.Status{Generation: desired.Generation, Stage: stage})
	return err
}

func TestDesiredChangeWakesHostWithoutStatusWrite(t *testing.T) {
	initialStatusObserved := make(chan struct{})
	var observedOnce sync.Once
	f := newFixtureWithOnChange(t, func() { observedOnce.Do(func() { close(initialStatusObserved) }) })
	select {
	case <-initialStatusObserved:
	case <-time.After(5 * time.Second):
		t.Fatal("initial app-server status was not observed")
	}
	f.host.mu.Lock()
	f.host.initializing = oneoff.New(f.ctx)
	f.host.initializing.Cancel()
	f.host.mu.Unlock()
	ctx, cancel := context.WithTimeout(f.ctx, 5*time.Second)
	defer cancel()
	// RequestLocked uses the ordinary context. The wrapper's Done method is
	// reached only by waitFor, after it reads the Running Desired state.
	requestAdmission := f.host.deps.RequestAdmission
	f.host.deps.RequestAdmission = func(_ context.Context, effect func(context.Context) error) error {
		return requestAdmission(ctx, effect)
	}
	parked := make(chan struct{})
	release := make(chan struct{})
	waitCtx := &waitSubscriptionContext{Context: ctx, parked: parked, release: release}
	var releaseOnce sync.Once
	releaseWait := func() { releaseOnce.Do(func() { close(release) }) }
	defer releaseWait()
	result := make(chan error, 1)
	go func() { _, err := f.host.Host(waitCtx, nil); result <- err }()
	select {
	case <-parked:
	case <-ctx.Done():
		t.Fatal("host did not reach readiness wait")
	}
	if err := lifecycle.PublishEnvironmentDesired(f.ctx, f.store, lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	releaseWait()
	select {
	case err := <-result:
		if err == nil || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("host wait after Quiesce = %v", err)
		}
	case <-ctx.Done():
		t.Fatal("host missed Desired change with unchanged status")
	}
	current, _, err := ReadStatus(f.ctx, f.store)
	if err != nil {
		t.Fatal(err)
	}
	if current.Phase != Stopped {
		t.Fatalf("status changed during the no-op Quiesce: %+v", current)
	}
}

func waitForStatus(t *testing.T, f *fixture, predicate func(Status) bool) Status {
	t.Helper()
	deadline := time.After(15 * time.Second)
	tick := time.NewTicker(time.Millisecond)
	defer tick.Stop()
	for {
		current, _, err := ReadStatus(f.ctx, f.store)
		if err != nil {
			t.Fatal(err)
		}
		if predicate(current) {
			return current
		}
		select {
		case <-tick.C:
		case <-deadline:
			t.Fatalf("component runtime did not settle: %+v", current)
		}
	}
}

func TestHostProgressesWithoutObserverAndDrainsEarlyFrames(t *testing.T) {
	f := newFixture(t)
	frames := make(chan string, 1)
	ended := make(chan int, 1)
	owner := &Attachment{ID: "owner", Frames: func(_ context.Context, lines [][]byte) error {
		for _, line := range lines {
			frames <- string(line)
		}
		return nil
	}, Stderr: func(context.Context, string) error { return nil }, End: func(_ context.Context, code int) error { ended <- code; return nil }}
	ctx, cancel := context.WithTimeout(f.ctx, 15*time.Second)
	defer cancel()
	p, err := f.host.Host(ctx, owner)
	if err != nil {
		t.Fatal(err)
	}
	if p.ID() == "" || !p.Running() {
		t.Fatal("host returned no running process")
	}
	select {
	case frame := <-frames:
		if !strings.Contains(frame, "test/early") {
			t.Fatalf("startup owner received %q", frame)
		}
	case <-ctx.Done():
		t.Fatal("startup owner did not receive the early frame")
	}
	late := &Attachment{ID: "late"}
	if !p.host(late) {
		t.Fatal("live process rejected attachment")
	}
	hosts := p.beginExit()
	if p.host(&Attachment{ID: "after-cutover"}) {
		t.Fatal("process accepted attachment after exit cutover")
	}
	if len(hosts) != 2 || hosts[0] != owner || hosts[1] != late {
		t.Fatalf("exit snapshot = %v", hosts)
	}
	// The late attachment was only for the cutover assertion; its exit callback is not needed.
	p.Release(late)
	if err := lifecycle.PublishEnvironmentDesired(f.ctx, f.store, lifecycle.EnvironmentDesired{Mode: lifecycle.Closing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	waitForStatus(t, f, func(current Status) bool { return current.Phase == Stopped && current.ProcessID == "" })
	select {
	case <-ended:
	case <-ctx.Done():
		t.Fatal("exit callback did not settle before publication")
	}
}

func TestFailedDrainRetainsOwnership(t *testing.T) {
	f := newFixture(t)
	owner := &Attachment{ID: "owner", Frames: func(context.Context, [][]byte) error { return nil }, Stderr: func(context.Context, string) error { return nil }, End: func(context.Context, int) error { return nil }}
	ctx, cancel := context.WithTimeout(f.ctx, 15*time.Second)
	defer cancel()
	p, err := f.host.Host(ctx, owner)
	if err != nil {
		t.Fatal(err)
	}
	stopFailure := errors.New("fence rejected")
	f.host.mu.Lock()
	f.host.drainEffect = func(context.Context) error { return stopFailure }
	f.host.mu.Unlock()
	if err := lifecycle.PublishEnvironmentDesired(f.ctx, f.store, lifecycle.EnvironmentDesired{Mode: lifecycle.Closing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	failed := waitForStatus(t, f, func(current Status) bool { return current.StopError != "" })
	if !strings.Contains(failed.StopError, stopFailure.Error()) || !p.Running() {
		t.Fatalf("failed drain lost ownership: %+v", failed)
	}
	if err := p.process.Stop(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-p.ended:
	case <-ctx.Done():
		t.Fatal("readers did not settle after cleanup")
	}
}

func TestOperationCompletionPublicationPrecedesConsumption(t *testing.T) {
	store := &operationSignalState{State: state.WrapCore(namespaced.NewState(inmem.Build)), blockRevision: 2, publishing: make(chan struct{}), release: make(chan struct{})}
	host := operationHost(t, store, func(err error) { t.Error(err) })
	if err := host.startOperation(&host.initializing, func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	<-store.publishing
	slot := host.initializing
	if slot.Finished() {
		t.Fatal("operation finished before completion publication")
	}
	if host.mu.TryLock() {
		host.mu.Unlock()
		t.Fatal("completion publication did not hold the consumption mutex")
	}
	attempting := make(chan struct{})
	consumed := make(chan *oneoff.Operation, 1)
	go func() { close(attempting); op, _ := host.takeOperation(&host.initializing); consumed <- op }()
	<-attempting
	select {
	case <-consumed:
		t.Fatal("operation consumed before completion publication")
	default:
	}
	close(store.release)
	op := <-consumed
	host.deps.Workers.Wait()
	if op == nil || op.Err() != nil || host.initializing != nil {
		t.Fatalf("operation=%v slot=%v", op, host.initializing)
	}
	input, _, err := ReadLifecycleSignal(t.Context(), store)
	if err != nil || input.OperationRevision != 2 || input.Demand != 0 {
		t.Fatalf("signal=%+v error=%v", input, err)
	}
}

func TestOperationCompletionPublicationFailureRetainsWorkError(t *testing.T) {
	publicationErr := errors.New("completion signal failed")
	workErr := errors.New("initialization failed")
	store := &operationSignalState{State: state.WrapCore(namespaced.NewState(inmem.Build)), failRevision: 2, err: publicationErr}
	reported := make(chan error, 1)
	host := operationHost(t, store, func(err error) { reported <- err })
	if err := host.startOperation(&host.initializing, func(context.Context) error { return workErr }); err != nil {
		t.Fatal(err)
	}
	host.deps.Workers.Wait()
	op, done := host.takeOperation(&host.initializing)
	if !done || !errors.Is(op.Err(), workErr) || !errors.Is(op.Err(), publicationErr) {
		t.Fatalf("operation=%v done=%t", op, done)
	}
	select {
	case err := <-reported:
		if !errors.Is(err, publicationErr) {
			t.Fatal(err)
		}
	default:
		t.Fatal("completion publication failure was not reported")
	}
}

func TestOperationRevisionSurvivesConcurrentDemandReset(t *testing.T) {
	for _, revision := range []uint64{1, 2} {
		t.Run(fmt.Sprint(revision), func(t *testing.T) {
			store := &operationSignalState{State: state.WrapCore(namespaced.NewState(inmem.Build)), blockRevision: revision, publishing: make(chan struct{}), release: make(chan struct{})}
			host := operationHost(t, store, func(err error) { t.Error(err) })
			if err := host.updateLifecycleSignal(t.Context(), func(input *LifecycleSignal) { input.Demand = 7 }); err != nil {
				t.Fatal(err)
			}
			started := make(chan error, 1)
			go func() { started <- host.startOperation(&host.initializing, func(context.Context) error { return nil }) }()
			<-store.publishing
			if err := host.ResetDemand(t.Context()); err != nil {
				t.Fatal(err)
			}
			close(store.release)
			if err := <-started; err != nil {
				t.Fatal(err)
			}
			host.deps.Workers.Wait()
			input, _, err := ReadLifecycleSignal(t.Context(), store)
			if err != nil || input.OperationRevision != 2 || input.Demand != 0 {
				t.Fatalf("signal=%+v error=%v", input, err)
			}
		})
	}
}

func TestOperationRevisionPreservesConcurrentDemand(t *testing.T) {
	store := &operationSignalState{State: state.WrapCore(namespaced.NewState(inmem.Build)), blockRevision: 1, publishing: make(chan struct{}), release: make(chan struct{})}
	host := operationHost(t, store, func(err error) { t.Error(err) })
	if err := lifecycle.PublishEnvironmentDesired(t.Context(), store, lifecycle.EnvironmentDesired{Mode: lifecycle.Running, Generation: 1}); err != nil {
		t.Fatal(err)
	}
	started := make(chan error, 1)
	go func() { started <- host.startOperation(&host.initializing, func(context.Context) error { return nil }) }()
	<-store.publishing
	demand, err := host.RequestLocked(t.Context(), nil)
	if err != nil || demand != 1 {
		t.Fatalf("demand=%d error=%v", demand, err)
	}
	close(store.release)
	if err := <-started; err != nil {
		t.Fatal(err)
	}
	host.deps.Workers.Wait()
	input, _, err := ReadLifecycleSignal(t.Context(), store)
	if err != nil || input.OperationRevision != 2 || input.Demand != 1 {
		t.Fatalf("signal=%+v error=%v", input, err)
	}
}

func TestDemandResetRetriesWithoutLosingOperationRevision(t *testing.T) {
	store := &operationSignalState{State: state.WrapCore(namespaced.NewState(inmem.Build)), blockRevision: 0, publishing: make(chan struct{}), release: make(chan struct{})}
	host := operationHost(t, store.State, func(err error) { t.Error(err) })
	if err := host.updateLifecycleSignal(t.Context(), func(input *LifecycleSignal) { input.Demand = 7 }); err != nil {
		t.Fatal(err)
	}
	host.deps.State = store
	reset := make(chan error, 1)
	go func() { reset <- host.ResetDemand(t.Context()) }()
	<-store.publishing
	if err := host.startOperation(&host.initializing, func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	host.deps.Workers.Wait()
	close(store.release)
	if err := <-reset; err != nil {
		t.Fatal(err)
	}
	input, _, err := ReadLifecycleSignal(t.Context(), store)
	if err != nil || input.OperationRevision != 2 || input.Demand != 0 {
		t.Fatalf("signal=%+v error=%v", input, err)
	}
}

func TestOperationRevisionPreservesConcurrentResetAndNativeExit(t *testing.T) {
	var store *operationSignalState
	var blockAt atomic.Uint64
	blockAt.Store(^uint64(0))
	f := newFixtureWithConfiguration(t, nil, func(host *Host) {
		store = &operationSignalState{State: host.deps.State, blockAt: &blockAt, publishing: make(chan struct{}), release: make(chan struct{})}
		host.deps.State = store
	})
	ctx, cancel := context.WithTimeout(f.ctx, lifecycleTestTimeout)
	defer cancel()
	process, err := f.host.Host(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	signal, _, err := ReadLifecycleSignal(ctx, f.store)
	if err != nil {
		t.Fatal(err)
	}
	blockAt.Store(signal.OperationRevision + 1)
	started := make(chan error, 1)
	go func() { started <- f.host.startOperation(&f.host.draining, func(context.Context) error { return nil }) }()
	<-store.publishing
	if err := f.host.ResetDemand(ctx); err != nil {
		t.Fatal(err)
	}
	if err := process.process.Stop(ctx); err != nil {
		t.Fatal(err)
	}
	_, err = safe.StateWatchFor[*typed.Resource[LifecycleSignal, lifecycle.Extension[LifecycleSignal]]](ctx, f.store, lifecycle.ResourceFor(ResourceID, LifecycleSignal{}).Metadata(), state.WithEventTypes(state.Created, state.Updated), state.WithCondition(func(value resource.Resource) (bool, error) {
		return value.(*typed.Resource[LifecycleSignal, lifecycle.Extension[LifecycleSignal]]).TypedSpec().ExitSequence > signal.ExitSequence, nil
	}))
	if err != nil {
		t.Fatal(err)
	}
	close(store.release)
	if err := <-started; err != nil {
		t.Fatal(err)
	}
	waitForStatus(t, f, func(current Status) bool {
		return current.Phase == Stopped && current.ExitSequence > signal.ExitSequence
	})
	input, _, err := ReadLifecycleSignal(ctx, f.store)
	if err != nil || input.OperationRevision != signal.OperationRevision+2 || input.Demand != 0 || input.ExitProcessID != process.ID() {
		t.Fatalf("signal=%+v error=%v", input, err)
	}
}

func TestOperationCompletionWakesControllerWithoutDemand(t *testing.T) {
	f := newFixture(t)
	waitForStatus(t, f, func(current Status) bool { return current.EnvironmentGeneration == 1 })
	release := make(chan struct{})
	workErr := errors.New("test initialization refused")
	if err := f.host.startOperation(&f.host.initializing, func(context.Context) error { <-release; return workErr }); err != nil {
		t.Fatal(err)
	}
	close(release)
	current := waitForStatus(t, f, func(current Status) bool { return current.Error == workErr.Error() })
	signal, _, err := ReadLifecycleSignal(f.ctx, f.store)
	if err != nil || signal != (LifecycleSignal{OperationRevision: 2}) {
		t.Fatalf("signal=%+v error=%v", signal, err)
	}
	if current.Phase != Stopped || current.ObservedDemand != 0 || current.ProcessID != "" {
		t.Fatalf("status=%+v", current)
	}
	f.host.mu.Lock()
	remaining := f.host.initializing
	f.host.mu.Unlock()
	if remaining != nil {
		t.Fatal("controller did not consume completed operation")
	}
}

func TestMissingLifecycleSignalFailsClosed(t *testing.T) {
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	reported := make(chan error, 2)
	host := &Host{deps: Dependencies{Context: t.Context(), State: store, Workers: &sync.WaitGroup{}, OnError: func(err error) { reported <- err }}, changed: make(chan struct{})}
	if err := lifecycle.PublishEnvironmentDesired(t.Context(), store, lifecycle.EnvironmentDesired{Mode: lifecycle.Running, Generation: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := host.RequestLocked(t.Context(), nil); !state.IsNotFoundError(err) {
		t.Fatalf("request error=%v", err)
	}
	var effects atomic.Int32
	if err := host.startOperation(&host.initializing, func(context.Context) error { effects.Add(1); return nil }); !state.IsNotFoundError(err) {
		t.Fatalf("start error=%v", err)
	}
	host.deps.Workers.Wait()
	if effects.Load() != 0 || host.initializing != nil {
		t.Fatal("missing lifecycle signal admitted work")
	}
	host.publishExit(&Process{})
	for range 2 {
		select {
		case err := <-reported:
			if !state.IsNotFoundError(err) {
				t.Fatal(err)
			}
		default:
			t.Fatal("missing lifecycle signal error was not reported")
		}
	}
	if _, found, err := ReadLifecycleSignal(t.Context(), store); err != nil || found {
		t.Fatalf("signal created after failure: found=%t error=%v", found, err)
	}
}

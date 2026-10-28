package codexappserver

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && os.Args[1] == supervision.SupervisorMode {
		os.Exit(supervision.SupervisorMain(os.Args[2:]))
	}
	os.Exit(m.Run())
}

type fixture struct {
	ctx     context.Context
	cancel  context.CancelFunc
	store   state.State
	host    *Host
	workers sync.WaitGroup
	runDone chan error
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
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	f := &fixture{ctx: ctx, cancel: cancel, store: state.WrapCore(namespaced.NewState(inmem.Build)), runDone: make(chan error, 1)}
	engine, err := runtime.NewRuntime(f.store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	processes := supervision.NewHost(ctx, f.store, lifecycle.Namespace, &f.workers, func(err error) { t.Errorf("process host: %v", err) })
	admit := func(ctx context.Context, expected lifecycle.Desired, effect func(context.Context) error) error {
		current, found, err := lifecycle.ReadDesired(ctx, f.store)
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
	f.host, err = NewHost(harness.LaunchConfig{Binary: binary, BaseURL: "https://codex.example.invalid/v1", Token: "test"}, Dependencies{
		Context: ctx, State: f.store, Workers: &f.workers, Processes: processes,
		Supervisor: supervision.NewSubprocessSupervisor(nil), StateDir: t.TempDir(),
		RequestAdmission: func(ctx context.Context, effect func(context.Context) error) error {
			current, found, err := lifecycle.ReadDesired(ctx, f.store)
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
	if err := f.host.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	if err := lifecycle.PublishDesired(ctx, f.store, lifecycle.Desired{Mode: lifecycle.Running, Generation: 1}); err != nil {
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
		Inputs:      []controller.Input{{Namespace: lifecycle.Namespace, Type: lifecycle.DesiredStateType, Kind: controller.InputQPrimary}},
		Outputs:     []controller.Output{{Type: lifecycle.AggregateStatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*aggregateController) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, _ controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	return []resource.Pointer{resource.NewMetadata(lifecycle.Namespace, lifecycle.DesiredStateType, lifecycle.ResourceID, resource.VersionUndefined)}, nil
}

func (*aggregateController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	desired, found, err := lifecycle.ReadDesired(ctx, r)
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

func TestDesiredChangeWakesHostWithoutRuntimeWrite(t *testing.T) {
	initialRuntimeObserved := make(chan struct{})
	var observedOnce sync.Once
	f := newFixtureWithOnChange(t, func() { observedOnce.Do(func() { close(initialRuntimeObserved) }) })
	select {
	case <-initialRuntimeObserved:
	case <-time.After(5 * time.Second):
		t.Fatal("initial app-server runtime was not observed")
	}
	f.host.mu.Lock()
	f.host.initializing = &operation{done: make(chan struct{})}
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
	if err := lifecycle.PublishDesired(f.ctx, f.store, lifecycle.Desired{Mode: lifecycle.Quiescing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	releaseWait()
	select {
	case err := <-result:
		if err == nil || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("host wait after Quiesce = %v", err)
		}
	case <-ctx.Done():
		t.Fatal("host missed Desired change with unchanged Runtime")
	}
	current, _, err := ReadRuntime(f.ctx, f.store)
	if err != nil {
		t.Fatal(err)
	}
	if current.Phase != Stopped {
		t.Fatalf("Runtime changed during the no-op Quiesce: %+v", current)
	}
}

func waitForRuntime(t *testing.T, f *fixture, predicate func(Runtime) bool) Runtime {
	t.Helper()
	deadline := time.After(15 * time.Second)
	tick := time.NewTicker(time.Millisecond)
	defer tick.Stop()
	for {
		current, _, err := ReadRuntime(f.ctx, f.store)
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
	if err := lifecycle.PublishDesired(f.ctx, f.store, lifecycle.Desired{Mode: lifecycle.Closing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	waitForRuntime(t, f, func(current Runtime) bool { return current.Phase == Stopped && current.ProcessID == "" })
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
	if err := lifecycle.PublishDesired(f.ctx, f.store, lifecycle.Desired{Mode: lifecycle.Closing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	failed := waitForRuntime(t, f, func(current Runtime) bool { return current.StopError != "" })
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

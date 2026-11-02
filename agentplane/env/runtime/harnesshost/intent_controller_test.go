package harnesshost

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

func TestHostIntentDecisionFailsClosedOnUnknownEffect(t *testing.T) {
	old := runnerlifecycle.HostIntent{RuntimeBootID: "runtime", HostBootID: "host", Generation: 1}
	next := old
	next.Generation++
	if shouldApplyHostIntent(true, next, true, runnerlifecycle.HostIntentStatus{Intent: old}) {
		t.Fatal("a newer intent replayed past an uncertain prior ApplyIntent")
	}
	if shouldApplyHostIntent(true, next, true, runnerlifecycle.HostIntentStatus{Intent: old, Error: "failed"}) {
		t.Fatal("a newer intent passed a failed prior ApplyIntent")
	}
	if !shouldApplyHostIntent(true, next, true, runnerlifecycle.HostIntentStatus{Intent: old, Applied: true}) {
		t.Fatal("a completed prior intent blocked the next generation")
	}
	if shouldApplyHostIntent(true, old, true, runnerlifecycle.HostIntentStatus{Intent: old, Applied: true}) {
		t.Fatal("an already applied intent was repeated")
	}
}

func TestHostIntentAdmissionRequiresStartupAndExactConnection(t *testing.T) {
	desired := runnerlifecycle.HostIntent{RuntimeBootID: "runtime", HostBootID: "host", Generation: 1}
	controller := &hostIntentController{runner: &Runner{bootID: "host"}}
	if controller.eligible(desired) {
		t.Fatal("intent applied before Session startup")
	}
	controller.runner.startupComplete = true
	connection := &runtimeConnection{runtimeBootID: "runtime", hostBootID: "host", connected: true}
	controller.runner.runtimeConnection.Store(connection)
	if !controller.runner.RuntimeConnectionMatches(desired.RuntimeBootID, desired.HostBootID) {
		t.Fatal("exact connected Host was rejected")
	}
	if !controller.eligible(desired) {
		t.Fatal("connected Host rejected intent after startup")
	}
	for name, mutate := range map[string]func(*runtimeConnection){
		"disconnected":         func(v *runtimeConnection) { v.connected = false },
		"Runtime boot changed": func(v *runtimeConnection) { v.runtimeBootID = "other-runtime" },
		"Host boot changed":    func(v *runtimeConnection) { v.hostBootID = "other-host" },
	} {
		t.Run(name, func(t *testing.T) {
			invalid := *connection
			mutate(&invalid)
			controller.runner.runtimeConnection.Store(&invalid)
			if controller.eligible(desired) {
				t.Fatal("invalid Host connection admitted intent")
			}
		})
	}
	controller.runner.runtimeConnection.Store(nil)
	if controller.eligible(desired) {
		t.Fatal("missing connection admitted intent")
	}
}

func TestHostFailureCannotReopenRuntimeConnection(t *testing.T) {
	runner := &Runner{fatal: make(chan error, 1)}
	runner.SetRuntimeConnection("runtime", "host", true)
	runner.fail(context.Canceled)
	runner.SetRuntimeConnection("runtime", "host", true)
	if runner.RuntimeConnectionMatches("runtime", "host") {
		t.Fatal("a failed Host was reconnected for intent application")
	}
}

type heldApplyingState struct {
	state.State
	marked  chan struct{}
	release chan struct{}
	once    sync.Once
}

func (s *heldApplyingState) Create(ctx context.Context, value resource.Resource, opts ...state.CreateOption) error {
	if err := s.State.Create(ctx, value, opts...); err != nil {
		return err
	}
	if value.Metadata().Type() == runnerlifecycle.HostIntentStatusType {
		s.once.Do(func() { close(s.marked) })
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func TestHostDisconnectAfterApplyingMarkerSkipsNativeEffect(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store := &heldApplyingState{State: state.WrapCore(namespaced.NewState(inmem.Build)), marked: make(chan struct{}), release: make(chan struct{})}
	engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	runner := &Runner{bootID: "host", startupComplete: true, fatal: make(chan error, 1)}
	runner.SetRuntimeConnection("runtime", "host", true)
	var calls atomic.Int32
	controller := &hostIntentController{runner: runner, apply: func(context.Context, *runtimepb.LifecycleIntent) error { calls.Add(1); return nil }}
	if err := engine.RegisterController(controller); err != nil {
		t.Fatal(err)
	}
	desired := runnerlifecycle.HostIntent{RuntimeBootID: "runtime", HostBootID: "host", Generation: 1, SandboxUID: "sandbox"}
	if err := cosi.Publish(ctx, store, runnerlifecycle.HostIntentResource(desired), desired, runnerlifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	runCtx, stop := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- engine.Run(runCtx) }()
	defer func() { stop(); <-done }()
	select {
	case <-store.marked:
	case <-ctx.Done():
		t.Fatal("Host never recorded Applying")
	}
	runner.SetRuntimeConnection("runtime", "host", false)
	close(store.release)
	_, err = store.WatchFor(ctx, runnerlifecycle.HostIntentStatusResource(runnerlifecycle.HostIntentStatus{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			status := value.(interface {
				TypedSpec() *runnerlifecycle.HostIntentStatus
			}).TypedSpec()
			return status.Error != "", nil
		}))
	if err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 0 {
		t.Fatal("disconnected Host applied a native intent")
	}
}

func TestHostIntentControllerFailsOnInterruptedApplication(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	runner := &Runner{bootID: "host", startupComplete: true, fatal: make(chan error, 1)}
	runner.SetRuntimeConnection("runtime", "host", true)
	var calls atomic.Int32
	if err := engine.RegisterController(&hostIntentController{runner: runner, apply: func(context.Context, *runtimepb.LifecycleIntent) error {
		calls.Add(1)
		return nil
	}}); err != nil {
		t.Fatal(err)
	}
	old := runnerlifecycle.HostIntent{RuntimeBootID: "runtime", HostBootID: "host", Generation: 1}
	current := runnerlifecycle.HostIntentStatus{Intent: old}
	if err := cosi.Publish(ctx, store, runnerlifecycle.HostIntentStatusResource(current), current, "harness-host-intent"); err != nil {
		t.Fatal(err)
	}
	next := old
	next.Generation++
	if err := cosi.Publish(ctx, store, runnerlifecycle.HostIntentResource(next), next, runnerlifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	runCtx, stop := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- engine.Run(runCtx) }()
	defer func() { stop(); <-done }()
	select {
	case err := <-runner.fatal:
		if err == nil {
			t.Fatal("unknown prior effect did not fail the Host")
		}
	case <-ctx.Done():
		t.Fatal("Host did not report unknown prior effect")
	}
	if calls.Load() != 0 {
		t.Fatal("Host replayed an uncertain native intent")
	}
}

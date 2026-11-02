package harnesshost

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

func TestHostIntentDecisionFailsClosedOnUnknownEffect(t *testing.T) {
	old := lifecycle.HostIntent{RuntimeBootID: "runtime", Generation: 1}
	next := old
	next.Generation++
	if shouldApplyHostIntent(true, next, true, lifecycle.HostIntentStatus{Intent: old}) {
		t.Fatal("a newer intent replayed past an uncertain prior ApplyIntent")
	}
	if shouldApplyHostIntent(true, next, true, lifecycle.HostIntentStatus{Intent: old, Error: "failed"}) {
		t.Fatal("a newer intent passed a failed prior ApplyIntent")
	}
	if !shouldApplyHostIntent(true, next, true, lifecycle.HostIntentStatus{Intent: old, Applied: true}) {
		t.Fatal("a completed prior intent blocked the next generation")
	}
	if shouldApplyHostIntent(true, old, true, lifecycle.HostIntentStatus{Intent: old, Applied: true}) {
		t.Fatal("an already applied intent was repeated")
	}
}

func TestHostIntentAdmissionRequiresStartupAndExactConnection(t *testing.T) {
	desired := lifecycle.HostIntent{RuntimeBootID: "runtime", Generation: 1}
	controller := &hostIntentController{host: &Host{}}
	if controller.eligible(desired) {
		t.Fatal("intent applied before Session startup")
	}
	controller.host.startupComplete = true
	connection := &runtimeBinding{runtimeBootID: "runtime", active: true}
	controller.host.runtimeBinding.Store(connection)
	if !controller.host.RuntimeBindingMatches(desired.RuntimeBootID) {
		t.Fatal("exact active Host binding was rejected")
	}
	if !controller.eligible(desired) {
		t.Fatal("active Host rejected intent after startup")
	}
	for name, mutate := range map[string]func(*runtimeBinding){
		"disconnected":         func(v *runtimeBinding) { v.active = false },
		"Runtime boot changed": func(v *runtimeBinding) { v.runtimeBootID = "other-runtime" },
	} {
		t.Run(name, func(t *testing.T) {
			invalid := *connection
			mutate(&invalid)
			controller.host.runtimeBinding.Store(&invalid)
			if controller.eligible(desired) {
				t.Fatal("invalid Host connection admitted intent")
			}
		})
	}
	controller.host.runtimeBinding.Store(nil)
	if controller.eligible(desired) {
		t.Fatal("missing connection admitted intent")
	}
}

func TestHostIntentApplyingMarkerClosesPriorGenerationAdmission(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	previous := lifecycle.HostIntent{RuntimeBootID: "runtime", Generation: 1, SandboxUID: "sandbox", IncarnationUID: "incarnation", Mode: runtimepb.LifecycleIntent_RUNNING}
	next := previous
	next.Generation++
	runner := &Host{ctx: ctx, startupComplete: true, fatal: make(chan error, 1), lifecycle: &lifecycle{state: store}, intent: previous.Proto()}
	runner.ready.Store(true)
	runner.SetRuntimeBinding("runtime", true)
	entered, release := make(chan struct{}), make(chan struct{})
	var releaseOnce sync.Once
	var calls atomic.Int32
	if err := engine.RegisterController(&hostIntentController{host: runner, apply: func(ctx context.Context, _ *runtimepb.LifecycleIntent) error {
		calls.Add(1)
		close(entered)
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}}); err != nil {
		t.Fatal(err)
	}
	oldDesired := lifecycle.Desired{Mode: lifecycle.Running, Generation: previous.Generation}
	if err := lifecycle.PublishDesired(ctx, store, oldDesired); err != nil {
		t.Fatal(err)
	}
	oldStatus := lifecycle.HostIntentStatus{Intent: previous, Applied: true}
	if err := cosi.Publish(ctx, store, lifecycle.HostIntentStatusResource(oldStatus), oldStatus, "harness-host-intent"); err != nil {
		t.Fatal(err)
	}
	if err := cosi.Publish(ctx, store, lifecycle.HostIntentResource(next), next, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	runCtx, stop := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- engine.Run(runCtx) }()
	defer func() { releaseOnce.Do(func() { close(release) }); stop(); <-done }()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("Host intent did not reach native application")
	}
	current, found, err := cosi.Read(ctx, store, lifecycle.HostIntentStatusResource(lifecycle.HostIntentStatus{}))
	if err != nil || !found || current.Intent != next || current.Applied || current.Error != "" {
		t.Fatalf("Applying marker = %+v, found=%t, err=%v", current, found, err)
	}
	var effectRan bool
	if err := runner.admitLifecycleEffect(ctx, oldDesired, func(context.Context) error {
		effectRan = true
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if effectRan {
		t.Fatal("prior-generation effect was admitted after Applying became visible")
	}
	releaseOnce.Do(func() { close(release) })
	if applied := waitIntentStatus(t, ctx, store, next, true); !applied.Applied || calls.Load() != 1 {
		t.Fatalf("next intent = %+v, application count %d", applied, calls.Load())
	}
}

func TestHostFailureCannotReopenRuntimeBinding(t *testing.T) {
	runner := &Host{fatal: make(chan error, 1)}
	runner.SetRuntimeBinding("runtime", true)
	runner.fail(context.Canceled)
	runner.SetRuntimeBinding("runtime", true)
	if runner.RuntimeBindingMatches("runtime") {
		t.Fatal("a failed Host was reconnected for intent application")
	}
}

type observedIntentReadState struct {
	state.State
	read chan struct{}
	once sync.Once
}

func (s *observedIntentReadState) Get(ctx context.Context, ptr resource.Pointer, opts ...state.GetOption) (resource.Resource, error) {
	value, err := s.State.Get(ctx, ptr, opts...)
	if ptr.Type() == lifecycle.HostIntentStatusType {
		s.once.Do(func() { close(s.read) })
	}
	return value, err
}

func TestHostIntentPublishedBeforeStartupIsAppliedAfterRecovery(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store := &observedIntentReadState{State: state.WrapCore(namespaced.NewState(inmem.Build)), read: make(chan struct{})}
	engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	runner := &Host{fatal: make(chan error, 1), startupDone: make(chan struct{})}
	runner.SetRuntimeBinding("runtime", true)
	var calls atomic.Int32
	if err := engine.RegisterController(&hostIntentController{host: runner, apply: func(context.Context, *runtimepb.LifecycleIntent) error {
		calls.Add(1)
		return nil
	}}); err != nil {
		t.Fatal(err)
	}
	desired := lifecycle.HostIntent{RuntimeBootID: "runtime", Generation: 1}
	if err := cosi.Publish(ctx, store, lifecycle.HostIntentResource(desired), desired, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	runCtx, stop := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- engine.Run(runCtx) }()
	defer func() { stop(); <-done }()
	select {
	case <-store.read:
	case <-ctx.Done():
		t.Fatal("controller did not observe the intent before startup")
	}
	if calls.Load() != 0 {
		t.Fatal("intent applied before recovery")
	}
	runner.mu.Lock()
	runner.startupComplete = true
	runner.mu.Unlock()
	close(runner.startupDone)
	if got := waitIntentStatus(t, ctx, store, desired, true); !got.Applied || calls.Load() != 1 {
		t.Fatalf("intent after recovery = %+v, application count %d", got, calls.Load())
	}
}

type heldApplyingState struct {
	state.State
	marked    chan struct{}
	release   chan struct{}
	once      sync.Once
	createErr error
}

func (s *heldApplyingState) Create(ctx context.Context, value resource.Resource, opts ...state.CreateOption) error {
	if err := s.State.Create(ctx, value, opts...); err != nil {
		return err
	}
	if value.Metadata().Type() == lifecycle.HostIntentStatusType {
		if s.createErr != nil {
			return s.createErr
		}
		s.once.Do(func() { close(s.marked) })
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func TestHostIntentUnknownApplyingPublicationFailsClosed(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	publicationErr := errors.New("applying publication outcome unknown")
	store := &heldApplyingState{State: state.WrapCore(namespaced.NewState(inmem.Build)), createErr: publicationErr}
	engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	runner := &Host{ctx: ctx, startupComplete: true, fatal: make(chan error, 1), lifecycle: &lifecycle{state: store}}
	runner.ready.Store(true)
	runner.SetRuntimeBinding("runtime", true)
	var calls atomic.Int32
	if err := engine.RegisterController(&hostIntentController{host: runner, apply: func(context.Context, *runtimepb.LifecycleIntent) error {
		calls.Add(1)
		return nil
	}}); err != nil {
		t.Fatal(err)
	}
	desired := lifecycle.HostIntent{RuntimeBootID: "runtime", Generation: 1, SandboxUID: "sandbox", IncarnationUID: "incarnation", Mode: runtimepb.LifecycleIntent_RUNNING}
	if err := cosi.Publish(ctx, store, lifecycle.HostIntentResource(desired), desired, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	runCtx, stop := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- engine.Run(runCtx) }()
	defer func() { stop(); <-done }()
	select {
	case failure := <-runner.fatal:
		if !errors.Is(failure, publicationErr) {
			t.Fatalf("Host failure = %v, want publication cause", failure)
		}
	case <-ctx.Done():
		t.Fatal("Host did not report the uncertain Applying publication")
	}
	current, found, err := cosi.Read(ctx, store, lifecycle.HostIntentStatusResource(lifecycle.HostIntentStatus{}))
	if err != nil || !found || current.Intent != desired || current.Applied || current.Error != "" {
		t.Fatalf("uncertain Applying marker = %+v, found=%t, err=%v", current, found, err)
	}
	if runner.ready.Load() || runner.RuntimeBindingMatches("runtime") || calls.Load() != 0 {
		t.Fatalf("uncertain intent reopened admission or replayed: ready=%t, bound=%t, applications=%d", runner.ready.Load(), runner.RuntimeBindingMatches("runtime"), calls.Load())
	}
}

func TestHostDisconnectAfterApplyingMarkerSkipsNativeEffect(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store := &heldApplyingState{State: state.WrapCore(namespaced.NewState(inmem.Build)), marked: make(chan struct{}), release: make(chan struct{})}
	engine, err := cosiruntime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	runner := &Host{startupComplete: true, fatal: make(chan error, 1)}
	runner.SetRuntimeBinding("runtime", true)
	var calls atomic.Int32
	controller := &hostIntentController{host: runner, apply: func(context.Context, *runtimepb.LifecycleIntent) error { calls.Add(1); return nil }}
	if err := engine.RegisterController(controller); err != nil {
		t.Fatal(err)
	}
	desired := lifecycle.HostIntent{RuntimeBootID: "runtime", Generation: 1, SandboxUID: "sandbox"}
	if err := cosi.Publish(ctx, store, lifecycle.HostIntentResource(desired), desired, lifecycle.EnvironmentOwner); err != nil {
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
	runner.SetRuntimeBinding("runtime", false)
	close(store.release)
	_, err = store.WatchFor(ctx, lifecycle.HostIntentStatusResource(lifecycle.HostIntentStatus{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			status := value.(interface {
				TypedSpec() *lifecycle.HostIntentStatus
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
	runner := &Host{startupComplete: true, fatal: make(chan error, 1)}
	runner.SetRuntimeBinding("runtime", true)
	var calls atomic.Int32
	if err := engine.RegisterController(&hostIntentController{host: runner, apply: func(context.Context, *runtimepb.LifecycleIntent) error {
		calls.Add(1)
		return nil
	}}); err != nil {
		t.Fatal(err)
	}
	old := lifecycle.HostIntent{RuntimeBootID: "runtime", Generation: 1}
	current := lifecycle.HostIntentStatus{Intent: old}
	if err := cosi.Publish(ctx, store, lifecycle.HostIntentStatusResource(current), current, "harness-host-intent"); err != nil {
		t.Fatal(err)
	}
	next := old
	next.Generation++
	if err := cosi.Publish(ctx, store, lifecycle.HostIntentResource(next), next, lifecycle.EnvironmentOwner); err != nil {
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

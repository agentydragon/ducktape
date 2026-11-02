package runtime

import (
	"context"
	"errors"
	"sync"
	"testing"

	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	"google.golang.org/protobuf/proto"
)

func startHostIntentController(t *testing.T, r *Runtime) {
	t.Helper()
	r.lifecycleState = state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(r.lifecycleState, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterController(&hostReportObserver{runtime: r}); err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterController(&hostIntentController{runtime: r}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- engine.Run(ctx) }()
	t.Cleanup(func() { cancel(); <-done })
}

func publishAppliedHostIntent(t *testing.T, r *Runtime) {
	t.Helper()
	value := hostIntentStatus{Intent: intentValue(r.intent), Applied: true}
	if err := cosi.Publish(t.Context(), r.lifecycleState, hostIntentStatusResource(value), value, "environment-host-intent"); err != nil {
		t.Fatal(err)
	}
}

type heldHostPeer struct {
	mu           sync.Mutex
	applied      *runtimepb.LifecycleIntent
	started      chan struct{}
	release      chan struct{}
	holdFenced   bool
	state        state.State
	fenceStarted chan struct{}
	fenceRelease chan struct{}
}

func (p *heldHostPeer) BootID() string { return "host" }
func (p *heldHostPeer) ApplyIntent(ctx context.Context, intent *runtimepb.LifecycleIntent) error {
	p.mu.Lock()
	first := p.applied == nil
	p.mu.Unlock()
	if first {
		select {
		case p.started <- struct{}{}:
		case <-ctx.Done():
			return ctx.Err()
		}
		select {
		case <-p.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if p.holdFenced && intent.ExecutionFenced {
		select {
		case p.fenceStarted <- struct{}{}:
		case <-ctx.Done():
			return ctx.Err()
		}
		select {
		case <-p.fenceRelease:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	p.mu.Lock()
	p.applied = proto.Clone(intent).(*runtimepb.LifecycleIntent)
	p.mu.Unlock()
	report := &runtimepb.HostReport{Binding: proto.Clone(intent.Binding).(*runtimepb.Binding), Ready: true, NativeStopped: true, Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{}}
	value := lifecycle.HostReport{Intent: proto.Clone(intent).(*runtimepb.LifecycleIntent), Report: report}
	return cosi.Publish(ctx, p.state, lifecycle.HostReportResource(value), value, "harness-host-report")
}

func TestDirectHostFenceWaitsForHostToApplyIt(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	r := &Runtime{
		config: Config{Launch: configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{UID: "sandbox"}}},
		bootID: "runtime", changes: make(chan struct{}),
	}
	startHostIntentController(t, r)
	initialRelease := make(chan struct{})
	close(initialRelease)
	peer := &heldHostPeer{
		started: make(chan struct{}, 1), release: initialRelease, state: r.lifecycleState,
		holdFenced: true, fenceStarted: make(chan struct{}, 1), fenceRelease: make(chan struct{}),
	}
	done := make(chan error, 1)
	go func() { done <- r.connectHost(ctx, nil, peer) }()
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.host != nil && r.hostReportLocked() != nil && r.hostReportLocked().Ready
	}, "initial direct Host report")
	r.mu.Lock()
	binding := proto.Clone(r.intent.Binding).(*runtimepb.Binding)
	r.mu.Unlock()
	if err := r.setExecutionFenced(binding); err != nil {
		t.Fatal(err)
	}
	select {
	case <-peer.fenceStarted:
	case <-ctx.Done():
		t.Fatal("fenced intent was not sent to Host")
	}
	// The old report remains in COSI while the same-generation fence is applying.
	// Its exact intent must not acknowledge the fence.
	r.mu.Lock()
	premature := r.hostReportLocked()
	r.mu.Unlock()
	if premature != nil {
		t.Fatalf("unapplied same-generation fence restored ready report: %v", premature)
	}
	probe, cancelProbe := context.WithCancel(ctx)
	cancelProbe()
	if err := r.waitForExecutionFenced(probe, binding); !errors.Is(err, context.Canceled) {
		t.Fatalf("unapplied Execution fence acknowledged: %v", err)
	}
	close(peer.fenceRelease)
	if err := r.waitForExecutionFenced(ctx, binding); err != nil {
		t.Fatalf("Host-applied Execution fence was not acknowledged: %v", err)
	}
	// A byte-identical report is accepted only after the new exact intent is applied.
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.hostReportLocked() != nil && r.hostReportLocked().Ready
	}, "same-generation Host report after Execution fence")
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("coordinator exit = %v, want cancellation", err)
	}
}

func TestDirectHostIntentPublishedWhileApplyIsBlocked(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	r := &Runtime{
		config: Config{Launch: configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{UID: "sandbox"}}},
		bootID: "runtime", changes: make(chan struct{}),
	}
	startHostIntentController(t, r)
	peer := &heldHostPeer{started: make(chan struct{}, 1), release: make(chan struct{}), state: r.lifecycleState}
	done := make(chan error, 1)
	go func() { done <- r.connectHost(ctx, nil, peer) }()
	select {
	case <-peer.started:
	case <-ctx.Done():
		t.Fatal("initial direct Host intent was not applied")
	}
	// The first ApplyIntent is blocked. Publishing the next intent must still
	// acquire Runtime's lock and the coordinator must apply the latest version.
	r.mu.Lock()
	want := r.setIntentLocked(runtimepb.LifecycleIntent_INACTIVE, "", "", false)
	unexpected := r.hostReportLocked()
	r.mu.Unlock()
	if unexpected != nil {
		t.Fatalf("blocked old intent acknowledged newer generation: %v", unexpected)
	}
	close(peer.release)
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.host != nil && r.hostReportLocked() != nil && r.hostReportLocked().Ready && proto.Equal(r.hostReportLocked().Binding, want.Binding)
	}, "latest direct Host intent report")
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("coordinator exit = %v, want cancellation", err)
	}
}

func TestDirectHostReportRequiresExactAppliedIntent(t *testing.T) {
	binding := &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 2}
	host := &hostLink{bootID: "host", connected: true}
	r := &Runtime{host: host, intent: &runtimepb.LifecycleIntent{Binding: binding, Mode: runtimepb.LifecycleIntent_RUNNING}, changes: make(chan struct{})}
	r.lifecycleState = state.WrapCore(namespaced.NewState(inmem.Build))
	publishAppliedHostIntent(t, r)
	report := &runtimepb.HostReport{Binding: proto.Clone(binding).(*runtimepb.Binding), Ready: true, Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{}}
	staleIntent := proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
	staleIntent.Binding.Generation--
	value := lifecycle.HostReport{Intent: staleIntent, Report: report}
	if err := cosi.Publish(t.Context(), r.lifecycleState, lifecycle.HostReportResource(value), value, "harness-host-report"); err != nil {
		t.Fatal(err)
	}
	if r.hostReportLocked() != nil {
		t.Fatal("stale Host report acknowledged current generation")
	}
	value.Intent = proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
	if err := cosi.Publish(t.Context(), r.lifecycleState, lifecycle.HostReportResource(value), value, "harness-host-report"); err != nil {
		t.Fatal(err)
	}
	report.Ready = false
	if got := r.hostReportLocked(); got == nil || !got.Ready {
		t.Fatalf("published report was not immutable: %v", got)
	}
	r.intent.ExecutionFenced = true
	if r.hostReportLocked() != nil {
		t.Fatal("old report acknowledged same-generation fence")
	}
}

func TestDirectHostExecutionReadinessFollowsOwnerLifetime(t *testing.T) {
	r := &Runtime{config: Config{CodexExecServer: &codexexecserver.Config{}}, intent: &runtimepb.LifecycleIntent{IncarnationUid: "incarnation"}}
	ready, err := r.HostExecutionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness before Execution owner = %t, %v", ready, err)
	}
	handle := newControllerExecutionHandle(nil)
	r.executionOwner = &executionLifetime{handle: handle, uid: "incarnation", admitted: true}
	ready, err = r.HostExecutionReady(t.Context())
	if err != nil || !ready {
		t.Fatalf("readiness with active Execution owner = %t, %v", ready, err)
	}
	handle.mu.Lock()
	handle.expected = true
	handle.mu.Unlock()
	ready, err = r.HostExecutionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness after owner stop = %t, %v", ready, err)
	}
	handle.mu.Lock()
	handle.expected = false
	handle.mu.Unlock()
	r.executionOwner.admitted = false
	ready, err = r.HostExecutionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness during fenced recovery = %t, %v", ready, err)
	}

	r.executionOwner.admitted = true
	backendLife, backendLost := context.WithCancel(t.Context())
	service := &executionServiceHandle{executor: &executorpb.UnimplementedExecutorServer{}, done: make(chan struct{}), backendLife: backendLife}
	r.executionOwner = &executionLifetime{handle: service, uid: "incarnation", admitted: true}
	ready, err = r.HostExecutionReady(t.Context())
	if err != nil || !ready {
		t.Fatalf("readiness while embedded service is running = %t, %v", ready, err)
	}
	backendLost()
	ready, err = r.HostExecutionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness after backend loss before service exit = %t, %v", ready, err)
	}
	close(service.done)
	ready, err = r.HostExecutionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness after backend exit before owner removal = %t, %v", ready, err)
	}
}

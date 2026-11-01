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
	if err := engine.RegisterController(&hostReportController{runtime: r}); err != nil {
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
	fenceStarted chan struct{}
	fenceRelease chan struct{}
}

func (p *heldHostPeer) BootID() string                { return "host" }
func (p *heldHostPeer) ChangesSince() <-chan struct{} { return make(chan struct{}) }
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
	return nil
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
		started: make(chan struct{}, 1), release: initialRelease,
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
	sampledIntent := intentValue(r.intent)
	r.mu.Unlock()
	if err := r.setExecutionFenced(binding); err != nil {
		t.Fatal(err)
	}
	select {
	case <-peer.fenceStarted:
	case <-ctx.Done():
		t.Fatal("fenced intent was not sent to Host")
	}
	// Reports carry only the generation, so this old ready sample is byte-identical
	// to the sample after the fence. It must not reopen admission while Apply is held.
	oldReport, err := peer.Report()
	if err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	host := r.host
	r.mu.Unlock()
	if err := r.acceptReport(host, oldReport, sampledIntent); err != nil {
		t.Fatal(err)
	}
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
	// The peer deliberately returns the same report bytes after the fence.
	// Runtime cleared its copy when publishing the fence, so the coordinator
	// must deliver an identical report again after Host applies the intent.
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.host != nil && r.hostReportLocked() != nil && r.hostReportLocked().Ready && proto.Equal(r.hostReportLocked().Binding, binding)
	}, "same-generation Host report after Execution fence")
	// Application can also finish between sampling and accepting an old report.
	// Its generation still matches, but its exact sampled intent must not be retagged.
	oldReport.Ready = false
	if err := r.acceptReport(host, oldReport, sampledIntent); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	after := r.hostReportLocked()
	r.mu.Unlock()
	if after == nil || !after.Ready {
		t.Fatalf("delayed old report overwrote applied fence readiness: %v", after)
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("coordinator exit = %v, want cancellation", err)
	}
}

func (p *heldHostPeer) Report() (*runtimepb.HostReport, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.applied == nil {
		return nil, nil
	}
	return &runtimepb.HostReport{Binding: proto.Clone(p.applied.Binding).(*runtimepb.Binding), Ready: true, NativeStopped: true, Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{}}, nil
}

func TestDirectHostIntentPublishedWhileApplyIsBlocked(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	r := &Runtime{
		config: Config{Launch: configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{UID: "sandbox"}}},
		bootID: "runtime", changes: make(chan struct{}),
	}
	startHostIntentController(t, r)
	peer := &heldHostPeer{started: make(chan struct{}, 1), release: make(chan struct{})}
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

func TestDirectHostReportCopiesAndDropsStaleGeneration(t *testing.T) {
	binding := &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 2}
	host := &hostLink{bootID: "host", connected: true}
	r := &Runtime{
		host: host, intent: &runtimepb.LifecycleIntent{Binding: proto.Clone(binding).(*runtimepb.Binding), Mode: runtimepb.LifecycleIntent_RUNNING},
		changes: make(chan struct{}),
	}
	r.lifecycleState = state.WrapCore(namespaced.NewState(inmem.Build))
	publishAppliedHostIntent(t, r)
	stale := &runtimepb.HostReport{
		Binding: &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 1},
		Ready:   true, Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{},
	}
	if err := r.acceptReport(host, stale, intentValue(r.intent)); err != nil || r.hostReportLocked() != nil {
		t.Fatalf("stale report accepted: report=%v err=%v", r.hostReportLocked(), err)
	}
	report := &runtimepb.HostReport{Binding: binding, Ready: true, Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{}}
	if err := r.acceptReport(host, report, intentValue(r.intent)); err != nil {
		t.Fatal(err)
	}
	report.Binding.Generation = 3
	report.Ready = false
	if r.hostReportLocked().Binding.Generation != 2 || !r.hostReportLocked().Ready {
		t.Fatalf("Runtime retained caller-owned report: %v", r.hostReportLocked())
	}
}

func TestDirectHostExecutionReadinessFollowsOwnerLifetime(t *testing.T) {
	r := &Runtime{config: Config{CodexExecServer: &codexexecserver.Config{}}, intent: &runtimepb.LifecycleIntent{IncarnationUid: "incarnation"}}
	ready, err := r.HostExecutionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness before Execution owner = %t, %v", ready, err)
	}
	handle := &localExecutionHandle{}
	r.executionOwner = handle
	r.executionServer = &executorpb.UnimplementedExecutorServer{}
	r.executionUID = "incarnation"
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
	r.executionRecovering = true
	ready, err = r.HostExecutionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness during fenced recovery = %t, %v", ready, err)
	}

	r.executionRecovering = false
	backendLife, backendLost := context.WithCancel(t.Context())
	service := &executionServiceHandle{done: make(chan struct{}), backendLife: backendLife}
	r.executionOwner = service
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

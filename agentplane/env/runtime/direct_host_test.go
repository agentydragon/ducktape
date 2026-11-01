package runtime

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	"google.golang.org/protobuf/proto"
)

type heldHostPeer struct {
	mu      sync.Mutex
	applied *runtimepb.LifecycleIntent
	started chan struct{}
	release chan struct{}
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
	p.mu.Lock()
	p.applied = proto.Clone(intent).(*runtimepb.LifecycleIntent)
	p.mu.Unlock()
	return nil
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
	unexpected := r.host.report
	r.mu.Unlock()
	if unexpected != nil {
		t.Fatalf("blocked old intent acknowledged newer generation: %v", unexpected)
	}
	close(peer.release)
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.host != nil && r.host.report != nil && r.host.report.Ready && proto.Equal(r.host.report.Binding, want.Binding)
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
	stale := &runtimepb.HostReport{
		Binding: &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 1},
		Ready:   true, Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{},
	}
	if err := r.acceptReport(host, stale); err != nil || host.report != nil {
		t.Fatalf("stale report accepted: report=%v err=%v", host.report, err)
	}
	report := &runtimepb.HostReport{Binding: binding, Ready: true, Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{}}
	if err := r.acceptReport(host, report); err != nil {
		t.Fatal(err)
	}
	report.Binding.Generation = 3
	report.Ready = false
	if host.report.Binding.Generation != 2 || !host.report.Ready {
		t.Fatalf("Runtime retained caller-owned report: %v", host.report)
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
}

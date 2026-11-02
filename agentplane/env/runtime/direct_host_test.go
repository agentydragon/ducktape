package runtime

import (
	"context"
	"errors"
	"testing"

	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	"google.golang.org/protobuf/proto"
)

type failedHostIntentPublishState struct{ state.State }

func (s *failedHostIntentPublishState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if value.Metadata().Type() == lifecycle.HostIntentType {
		return errors.New("injected Host intent publication failure")
	}
	return s.State.Modify(ctx, value, update, opts...)
}

func TestHostIntentPublicationFailureRevokesNativeConnection(t *testing.T) {
	client := &harnesshost.Runner{}
	client.SetRuntimeConnection("runtime", "host", true)
	intent := &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 1}}
	r := &Runtime{
		bootID: "runtime", host: &hostLink{bootID: "host", client: client, connected: true}, intent: intent,
		lifecycleState: &failedHostIntentPublishState{State: state.WrapCore(namespaced.NewState(inmem.Build))},
		changes:        make(chan struct{}), fatal: make(chan error, 1),
	}
	r.mu.Lock()
	r.intentChangedLocked()
	r.mu.Unlock()
	if !r.failed || r.failureErr == nil {
		t.Fatal("failed intent publication did not fail Runtime")
	}
	if client.RuntimeConnectionMatches("runtime", "host") {
		t.Fatal("failed publication left Host admitted for native intent application")
	}
}

func startHostReportObserver(t *testing.T, r *Runtime) {
	t.Helper()
	r.lifecycleState = state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(r.lifecycleState, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterController(&hostReportObserver{runtime: r}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- engine.Run(ctx) }()
	t.Cleanup(func() { cancel(); <-done })
}

func publishAppliedHostIntent(t *testing.T, r *Runtime) {
	t.Helper()
	value := lifecycle.HostIntentStatus{Intent: lifecycle.HostIntentFromProto(r.intent), Applied: true}
	if err := cosi.Publish(t.Context(), r.lifecycleState, lifecycle.HostIntentStatusResource(value), value, "harness-host-intent"); err != nil {
		t.Fatal(err)
	}
}

func TestExecutionFenceWaitsForExactHostApplication(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	binding := &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 1}
	r := &Runtime{bootID: "runtime", host: &hostLink{bootID: "host", connected: true}, intent: &runtimepb.LifecycleIntent{Binding: binding, Mode: runtimepb.LifecycleIntent_QUIESCING}, changes: make(chan struct{}), fatal: make(chan error, 1)}
	startHostReportObserver(t, r)
	publishAppliedHostIntent(t, r)
	old := lifecycle.HostReport{Intent: proto.Clone(r.intent).(*runtimepb.LifecycleIntent), Report: &runtimepb.HostReport{Binding: binding, Ready: true, Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{}}}
	if err := cosi.Publish(ctx, r.lifecycleState, lifecycle.HostReportResource(old), old, "harness-host-report"); err != nil {
		t.Fatal(err)
	}
	if err := r.setExecutionFenced(binding); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	premature := r.hostReportLocked()
	r.mu.Unlock()
	if premature != nil {
		t.Fatalf("old report acknowledged same-generation fence: %v", premature)
	}
	canceled, stop := context.WithCancel(ctx)
	stop()
	if err := r.waitForExecutionFenced(canceled, binding); !errors.Is(err, context.Canceled) {
		t.Fatalf("unapplied fence wait = %v", err)
	}
	done := make(chan error, 1)
	go func() { done <- r.waitForExecutionFenced(ctx, binding) }()
	applied := lifecycle.HostIntentStatus{Intent: lifecycle.HostIntentFromProto(r.intent), Applied: true}
	if err := cosi.Publish(ctx, r.lifecycleState, lifecycle.HostIntentStatusResource(applied), applied, "harness-host-intent"); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("applied fence wait: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("Host intent status did not wake fence waiter")
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

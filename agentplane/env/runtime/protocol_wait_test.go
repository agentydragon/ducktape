package runtime

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	"google.golang.org/protobuf/proto"
)

type uncertainFenceHandle struct{ calls atomic.Int32 }

func (*uncertainFenceHandle) Executor() executorpb.ExecutorServer {
	return &executorpb.UnimplementedExecutorServer{}
}

func (h *uncertainFenceHandle) StopAndFence(context.Context) error {
	h.calls.Add(1)
	return errors.New("uncertain fence")
}

func TestUncertainExecutionFenceIsNotRetriedForBinding(t *testing.T) {
	binding := &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 1}
	handle := &uncertainFenceHandle{}
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
		return handle, nil
	})
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	r.host = &hostLink{connected: true}
	r.intent = &runtimepb.LifecycleIntent{Binding: binding, Mode: runtimepb.LifecycleIntent_QUIESCING}
	publishAppliedHostIntent(t, r)
	report := &runtimepb.HostReport{Binding: binding, Ready: true, NativeStopped: true}
	value := lifecycle.HostReport{Intent: proto.Clone(r.intent).(*runtimepb.LifecycleIntent), Report: report}
	if err := cosi.Publish(t.Context(), r.lifecycleState, lifecycle.HostReportResource(value), value, "harness-host-report"); err != nil {
		t.Fatal(err)
	}
	r.mu.Unlock()
	r.maybeFenceExecution(binding)
	select {
	case <-r.fatal:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("uncertain fence did not fail Runtime")
	}
	r.mu.Lock()
	retained := proto.Equal(r.fenceInFlight, binding)
	r.mu.Unlock()
	if !retained {
		t.Fatal("uncertain fence lost its binding marker and can be retried")
	}
	r.maybeFenceExecution(binding)
	if got := handle.calls.Load(); got != 1 {
		t.Fatalf("fence calls = %d, want 1", got)
	}
}

func TestUnmanagedExecutionOwnerCannotClaimFence(t *testing.T) {
	handle := &uncertainFenceHandle{}
	r := &Runtime{executionOwner: &executionLifetime{handle: handle}}
	if err := r.stopExecution(t.Context()); err == nil {
		t.Fatal("unmanaged Execution owner claimed fence proof")
	}
	if r.executionOwner == nil || r.executionOwner.handle != handle || handle.calls.Load() != 0 {
		t.Fatal("unmanaged Execution owner was stopped or discarded")
	}
}

func TestWaitForHostQuiescedWakesForFinalReportPublishedAfterSnapshot(t *testing.T) {
	binding := &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 1}
	host := &hostLink{connected: true}
	report := &runtimepb.HostReport{
		Binding: binding,
		Ready:   true,
		Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{
			QuiesceStage: environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_FLUSHING,
		},
	}
	r := &Runtime{
		changes: make(chan struct{}),
		host:    host,
		intent:  &runtimepb.LifecycleIntent{Binding: binding, Mode: runtimepb.LifecycleIntent_INACTIVE},
	}

	r.lifecycleState = state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(r.lifecycleState, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterController(&hostReportObserver{runtime: r}); err != nil {
		t.Fatal(err)
	}
	observerCtx, stopObserver := context.WithCancel(t.Context())
	observerDone := make(chan error, 1)
	go func() { observerDone <- engine.Run(observerCtx) }()
	t.Cleanup(func() { stopObserver(); <-observerDone })
	publishAppliedHostIntent(t, r)
	initialChange := r.changes
	value := lifecycle.HostReport{Intent: proto.Clone(r.intent).(*runtimepb.LifecycleIntent), Report: report}
	if err := cosi.Publish(t.Context(), r.lifecycleState, lifecycle.HostReportResource(value), value, "harness-host-report"); err != nil {
		t.Fatal(err)
	}

	select {
	case <-initialChange:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("initial report did not wake Runtime")
	}
	quiesced, changed, err := r.hostQuiescenceWaitSnapshot(binding)
	if err != nil {
		t.Fatalf("snapshot before final report: %v", err)
	}
	if quiesced {
		t.Fatal("FLUSHING report was treated as quiesced")
	}

	finalReport := &runtimepb.HostReport{
		Binding: binding,
		Ready:   true,
		Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{
			QuiesceStage: environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED,
		},
	}
	value.Report = finalReport
	if err := cosi.Publish(t.Context(), r.lifecycleState, lifecycle.HostReportResource(value), value, "harness-host-report"); err != nil {
		t.Fatalf("accept final report: %v", err)
	}
	select {
	case <-changed:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("final report did not wake the channel captured with the prior report check")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := r.waitForHostQuiesced(ctx, binding); err != nil {
		t.Fatalf("wait after final report: %v", err)
	}
}

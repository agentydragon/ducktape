package runtime

import (
	"context"
	"testing"

	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
)

func TestWaitForHostQuiescedWakesForFinalReportPublishedAfterSnapshot(t *testing.T) {
	binding := &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 1}
	host := &hostLink{connected: true}
	host.report = &runtimepb.HostReport{
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
	if err := r.acceptReport(host, finalReport); err != nil {
		t.Fatalf("accept final report: %v", err)
	}
	select {
	case <-changed:
	default:
		t.Fatal("final report did not wake the channel captured with the prior report check")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := r.waitForHostQuiesced(ctx, binding); err != nil {
		t.Fatalf("wait after final report: %v", err)
	}
}

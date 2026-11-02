package harnesshost

import (
	"context"
	"testing"

	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

type reportTestWriter struct {
	controller.Writer
	state state.State
}

func (w reportTestWriter) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, _ ...controller.ModifyOption) error {
	return w.state.Modify(ctx, value, update, state.WithUpdateOwner("harness-host-report"))
}

func TestHostReportPublicationSuppressesEquivalentProtobufUpdates(t *testing.T) {
	ctx := t.Context()
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	writer := reportTestWriter{state: store}
	first := harnesshostlifecycle.HostReport{
		Intent: &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: "host", Generation: 1}},
		Report: &runtimepb.HostReport{Ready: true, Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{}},
	}
	if err := harnesshostlifecycle.WriteHostReport(ctx, writer, first); err != nil {
		t.Fatal(err)
	}
	pointer := harnesshostlifecycle.HostReportResource(harnesshostlifecycle.HostReport{}).Metadata()
	initial, err := store.Get(ctx, pointer)
	if err != nil {
		t.Fatal(err)
	}
	equivalent := first.DeepCopy()
	equivalent.Report.Facts.AutosuspendBlockers = []*sandboxpb.AutosuspendBlocker{}
	if err := harnesshostlifecycle.WriteHostReport(ctx, writer, equivalent); err != nil {
		t.Fatal(err)
	}
	unchanged, err := store.Get(ctx, pointer)
	if err != nil {
		t.Fatal(err)
	}
	if !initial.Metadata().Version().Equal(unchanged.Metadata().Version()) {
		t.Fatal("semantically equivalent report produced an update")
	}
	// The fence is part of the exact applied intent even at the same generation.
	equivalent.Intent.ExecutionFenced = true
	if err := harnesshostlifecycle.WriteHostReport(ctx, writer, equivalent); err != nil {
		t.Fatal(err)
	}
	changed, err := store.Get(ctx, pointer)
	if err != nil {
		t.Fatal(err)
	}
	if changed.Metadata().Version().Equal(unchanged.Metadata().Version()) {
		t.Fatal("same-generation Execution fence did not publish a new report")
	}
}

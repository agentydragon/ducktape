package runtime

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

// A Host can persist the seal after planning observed no COSI resource. The outcome
// must be read after adoption, so that this branch still fences and acknowledges
// the exact sealed generation instead of starting an active successor.
func TestAdoptionBarrierObservesSealWrittenAfterPlanning(t *testing.T) {
	lifecycleState := state.WrapCore(namespaced.NewState(inmem.Build))
	environmentState, err := NewEnvironmentState(lifecycleState)
	if err != nil {
		t.Fatal(err)
	}
	if err := environmentState.AdoptIncarnation("incarnation"); err != nil {
		t.Fatal(err)
	}
	plan := adoptionPlan{oldUID: "incarnation", targetUID: "incarnation", sealed: false}
	if err := environmentState.EnterLameduck(); err != nil {
		t.Fatal(err)
	}
	c := &runtimeTransitionSequence{r: &Runtime{state: environmentState}, adoption: plan}
	outcome, err := c.runAdoptionBarrier(context.Background(), adoptionBarrier{kind: adoptionIncarnation})
	if err != nil {
		t.Fatal(err)
	}
	if outcome.adoption != adoptionSealedSuccessor {
		t.Fatalf("adoption outcome = %d, want sealed successor", outcome.adoption)
	}
	steps := c.successorBarriers(outcome.adoption)
	want := []lifecycle.RuntimeTransitionStage{
		lifecycle.RuntimeTransitionExecutionStarting,
		lifecycle.RuntimeTransitionDesiredPublishing,
		lifecycle.RuntimeTransitionHostReady,
		lifecycle.RuntimeTransitionNativeStopping,
		lifecycle.RuntimeTransitionExecutionFencing,
		lifecycle.RuntimeTransitionHistoryAcknowledging,
	}
	if len(steps) != len(want) {
		t.Fatalf("sealed successor has %d barriers, want %d", len(steps), len(want))
	}
	for i, step := range steps {
		if step.stage != want[i] {
			t.Fatalf("sealed successor barrier %d = %d, want %d", i, step.stage, want[i])
		}
	}
	if steps[4].adoption.binding != adoptionCurrentBinding || steps[4].adoption.triggerFence {
		t.Fatalf("sealed successor fence = %+v, want exact current binding without an early trigger", steps[4].adoption)
	}
}

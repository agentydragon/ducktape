package runtime

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/cosi-project/runtime/pkg/state/owned"
)

func TestRepeatedQuiesceRetriesOnlyErrorFromCurrentGeneration(t *testing.T) {
	for _, test := range []struct {
		name           string
		progress       *lifecycle.Status
		wantGeneration uint64
	}{
		{name: "missing status", wantGeneration: 7},
		{name: "healthy current status", progress: &lifecycle.Status{Generation: 7}, wantGeneration: 7},
		{name: "stale failure", progress: &lifecycle.Status{Generation: 6, Error: "stale"}, wantGeneration: 7},
		{name: "current failure", progress: &lifecycle.Status{Generation: 7, Error: "failed"}, wantGeneration: 8},
	} {
		t.Run(test.name, func(t *testing.T) {
			store := state.WrapCore(namespaced.NewState(inmem.Build))
			env, err := NewEnvironmentState(store)
			if err != nil {
				t.Fatal(err)
			}
			if err := env.AdoptIncarnation("incarnation"); err != nil {
				t.Fatal(err)
			}
			if err := env.EnterLameduck(); err != nil {
				t.Fatal(err)
			}
			desired := lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, IncarnationUID: "incarnation", SealUID: "incarnation", Generation: 7}
			if err := lifecycle.PublishEnvironmentDesired(t.Context(), store, desired); err != nil {
				t.Fatal(err)
			}
			if test.progress != nil {
				if _, err := lifecycle.WriteStatus(t.Context(), owned.New(store, "test-session-lifecycle"), *test.progress); err != nil {
					t.Fatal(err)
				}
			}
			r := &Runtime{state: env, lifecycleState: store, desired: desired, startupComplete: true, changes: make(chan struct{}), fatal: make(chan error, 1)}
			sequence := &runtimeTransitionSequence{r: r}
			if _, err := sequence.runQuiesceBarrier(t.Context()); err != nil {
				t.Fatal(err)
			}
			if r.desired.Generation != test.wantGeneration {
				t.Fatalf("quiesce generation = %d, want %d", r.desired.Generation, test.wantGeneration)
			}
		})
	}
}

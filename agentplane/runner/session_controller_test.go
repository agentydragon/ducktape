package runner

import (
	"context"
	"reflect"
	"slices"
	"strconv"
	"testing"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/gen/optional"
)

func TestSessionControllerUsesConfiguredReconcileConcurrency(t *testing.T) {
	for _, concurrency := range []int{16, 2048} {
		t.Run(strconv.Itoa(concurrency), func(t *testing.T) {
			c := &sessionLifecycleController{l: &lifecycle{runner: &Runner{config: Config{
				SessionReconcileConcurrency: concurrency,
			}}}}
			settings := c.Settings()
			want := optional.Some(uint(concurrency))
			if !reflect.DeepEqual(settings.Concurrency, want) {
				t.Fatalf("Session controller concurrency = %#v, want %#v", settings.Concurrency, want)
			}
		})
	}
}

func TestSessionControllerMapsAppServerRuntimeToCodexSessions(t *testing.T) {
	r := newBareRunner(t)
	for id, harness := range map[string]runnerpb.Harness{
		"claude-session": runnerpb.Harness_HARNESS_CLAUDE,
		"codex-session":  runnerpb.Harness_HARNESS_CODEX,
	} {
		if err := publishLifecycle(r.lifecycle, id, sessionInputSpec{Harness: harness}); err != nil {
			t.Fatal(err)
		}
	}
	c := &sessionLifecycleController{l: r.lifecycle}
	ctx := context.Background()
	input := reducedLifecycleMetadata{Pointer: resource.NewMetadata(runnerlifecycle.Namespace, codexAppServerRuntimeType, codexAppServerResourceID, resource.VersionUndefined)}
	mapped, err := c.MapInput(ctx, nil, sessionControllerTestRuntime{state: r.lifecycle.state}, input)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := pointerIDs(mapped), []string{"codex-session"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("app-server runtime mappings = %v, want %v", got, want)
	}

	input.Pointer = resource.NewMetadata(runnerlifecycle.Namespace, runnerlifecycle.AggregateStatusType, runnerlifecycle.ResourceID, resource.VersionUndefined)
	mapped, err = c.MapInput(ctx, nil, sessionControllerTestRuntime{state: r.lifecycle.state}, input)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := pointerIDs(mapped), []string{"claude-session", "codex-session"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Runner runtime mappings = %v, want %v", got, want)
	}
}

type reducedLifecycleMetadata struct{ resource.Pointer }

func (reducedLifecycleMetadata) Phase() resource.Phase    { return resource.PhaseRunning }
func (reducedLifecycleMetadata) Labels() *resource.Labels { return nil }
func (reducedLifecycleMetadata) FinalizersEmpty() bool    { return true }

type sessionControllerTestRuntime struct {
	controller.QRuntime
	state state.State
}

func (r sessionControllerTestRuntime) List(ctx context.Context, _ resource.Kind, opts ...state.ListOption) (resource.List, error) {
	return r.state.List(ctx, resource.NewMetadata(runnerlifecycle.Namespace, sessionInputType, "", resource.VersionUndefined), opts...)
}

func pointerIDs(pointers []resource.Pointer) []string {
	ids := make([]string, 0, len(pointers))
	for _, ptr := range pointers {
		ids = append(ids, ptr.ID())
	}
	slices.Sort(ids)
	return ids
}

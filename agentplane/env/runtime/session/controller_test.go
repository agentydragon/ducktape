package session

import (
	"context"
	"reflect"
	"slices"
	"strconv"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/codexappserver"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/siderolabs/gen/optional"
)

func TestSessionControllerUsesConfiguredReconcileConcurrency(t *testing.T) {
	for _, concurrency := range []int{16, 2048} {
		t.Run(strconv.Itoa(concurrency), func(t *testing.T) {
			c := &sessionLifecycleController{manager: &Manager{workers: concurrency}}
			settings := c.Settings()
			want := optional.Some(uint(concurrency))
			if !reflect.DeepEqual(settings.Concurrency, want) {
				t.Fatalf("Session controller concurrency = %#v, want %#v", settings.Concurrency, want)
			}
		})
	}
}

func TestSessionControllerMapsAppServerStatusToCodexSessions(t *testing.T) {
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	for id, harness := range map[string]sessionpb.Harness{
		"claude-session": sessionpb.Harness_HARNESS_CLAUDE,
		"codex-session":  sessionpb.Harness_HARNESS_CODEX,
	} {
		if err := lifecycle.Publish(context.Background(), resources, id, ReconcileSnapshot{Harness: harness}, lifecycle.EnvironmentOwner); err != nil {
			t.Fatal(err)
		}
	}
	c := &sessionLifecycleController{manager: &Manager{}}
	ctx := context.Background()
	input := reducedLifecycleMetadata{Pointer: resource.NewMetadata(lifecycle.Namespace, codexappserver.StatusType, codexappserver.ResourceID, resource.VersionUndefined)}
	mapped, err := c.MapInput(ctx, nil, sessionControllerTestRuntime{state: resources}, input)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := pointerIDs(mapped), []string{"codex-session"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("app-server status mappings = %v, want %v", got, want)
	}

	input.Pointer = resource.NewMetadata(lifecycle.Namespace, lifecycle.AggregateStatusType, lifecycle.ResourceID, resource.VersionUndefined)
	mapped, err = c.MapInput(ctx, nil, sessionControllerTestRuntime{state: resources}, input)
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
	return r.state.List(ctx, resource.NewMetadata(lifecycle.Namespace, ReconcileSnapshotType, "", resource.VersionUndefined), opts...)
}

func pointerIDs(pointers []resource.Pointer) []string {
	ids := make([]string, 0, len(pointers))
	for _, ptr := range pointers {
		ids = append(ids, ptr.ID())
	}
	slices.Sort(ids)
	return ids
}

func TestPreFenceWaitsForNativeExitCallbackAndItsInspection(t *testing.T) {
	// A native reader can enqueue reconciliation after the process has exited.
	// stopProcess must join that reader before ProcessStopped can be published.
	s := &Session{connection: &nativeConnection{}}
	s.connection.readers.Add(1)
	stopped := make(chan error, 1)
	go func() { stopped <- s.stopProcess(t.Context()) }()
	select {
	case err := <-stopped:
		t.Fatalf("process stop finished before native exit callback: %v", err)
	case <-time.After(20 * time.Millisecond):
	}
	status := LifecycleStatus{EnvironmentGeneration: 7, Phase: Halted, ProcessStopped: false}
	if s.preFenceSettled(status) {
		t.Fatal("pre-fence cleanup settled before native exit callback joined")
	}
	s.mu.Lock()
	s.reconciliations = append(s.reconciliations, &pendingReconciliation{})
	s.mu.Unlock()
	s.connection.readers.Done()
	select {
	case err := <-stopped:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("process stop did not join the native callback")
	}
	status.ProcessStopped = true
	if s.preFenceSettled(status) {
		t.Fatal("pre-fence cleanup settled with late inspection pending")
	}
	s.mu.Lock()
	s.reconciliations = nil
	s.mu.Unlock()
	if !s.preFenceSettled(status) {
		t.Fatal("pre-fence cleanup stayed blocked after callback and inspection settled")
	}
	status.Error = "inspection publication failed"
	if s.preFenceSettled(status) {
		t.Fatal("pre-fence cleanup settled after inspection failure")
	}
}

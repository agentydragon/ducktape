package runtime

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

func TestRuntimeCloseStatusRequiresExactGeneration(t *testing.T) {
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	r := &Runtime{
		lifecycleState:       store,
		lifecycleDone:        make(chan struct{}),
		lifecycleCoordinator: &runtimeCloseController{stopped: make(chan struct{})},
	}
	t.Cleanup(func() { close(r.lifecycleCoordinator.stopped) })
	stale := RuntimeCloseStatus{Generation: 4, Stage: runtimeCloseReadyToJoin}
	if err := cosi.Publish(t.Context(), store, runtimeCloseStatusResource(stale), stale, runtimeLifecycleControllerName); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- r.waitRuntimeCloseStatus(5) }()
	select {
	case err := <-done:
		t.Fatalf("stale generation completed Close: %v", err)
	default:
	}
	current := RuntimeCloseStatus{Generation: 5, Stage: runtimeCloseReadyToJoin}
	if err := cosi.Publish(t.Context(), store, runtimeCloseStatusResource(current), current, runtimeLifecycleControllerName); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("exact-generation Close status did not wake waiter")
	}
}

type closeStatusPublicationFailureState struct {
	state.CoreState
	err      error
	fail     *atomic.Bool
	attempts *atomic.Int32
}

func (s *closeStatusPublicationFailureState) Update(ctx context.Context, value resource.Resource, options ...state.UpdateOption) error {
	if value.Metadata().Type() == runtimeCloseStatusType {
		s.attempts.Add(1)
		if s.fail.Load() {
			return s.err
		}
	}
	return s.CoreState.Update(ctx, value, options...)
}

func TestRuntimeCloseTerminalStatusPublicationFailureRetainsOwner(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	store, closer, err := cosistate.Open(config.StateDir, lifecycle.Namespace, bootstrap.SelectionRegistration(), bootstrap.EventRegistration(), retained.SessionRecordRegistration(), retained.IngressAckRegistration(), cosistate.EnvironmentSealRegistration())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = closer.Close() })
	config.testBacking = closer
	publicationFailure := errors.New("injected Runtime Close status publication failure")
	var stops atomic.Int32
	var failPublication atomic.Bool
	var statusWrites atomic.Int32
	config.deps.stopSessions = func(context.Context) error {
		stops.Add(1)
		failPublication.Store(true)
		return errors.New("injected Session stop failure")
	}
	failedCore := &closeStatusPublicationFailureState{CoreState: store, err: publicationFailure, fail: &failPublication, attempts: &statusWrites}
	r := newLifecycleRunnerWithState(t, config, state.WrapCore(failedCore))
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	if err := r.Close(ctx); !errors.Is(err, publicationFailure) {
		t.Fatalf("Close error = %v, want status publication failure after %d status write attempts", err, statusWrites.Load())
	}
	if got := stops.Load(); got != 1 {
		t.Fatalf("Session stop calls = %d, want one", got)
	}
	status := mustRuntimeCloseStatus(t, r)
	if status.Stage != runtimeCloseSessionsJoining || status.Failure != nil {
		t.Fatalf("unpublished terminal status became visible: %+v", status)
	}
	if owner, err := openTestRuntimeStore(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("status publication failure released environment owner")
	}
}

type nonComparableCloseFailure struct{ details []string }

func (e nonComparableCloseFailure) Error() string { return strings.Join(e.details, ", ") }

func TestRuntimeCloseStatusPreservesNonComparableFailure(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	config.deps.stopSessions = func(context.Context) error {
		return nonComparableCloseFailure{details: []string{"Session stop unproved"}}
	}
	r := newLifecycleRuntime(t, config)
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	err := r.Close(ctx)
	if _, ok := err.(nonComparableCloseFailure); !ok {
		t.Fatalf("Close error = %T: %v, want original failure type", err, err)
	}
	status := mustRuntimeCloseStatus(t, r)
	if status.Stage != runtimeCloseFailed || status.Failure == nil {
		t.Fatalf("Close status = %+v", status)
	}
}

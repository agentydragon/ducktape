package runner

import (
	"context"
	"errors"
	"testing"
	"time"

	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

type failedLifecycleWatchState struct {
	state.State
	err error
}

func (s *failedLifecycleWatchState) WatchKindAggregated(context.Context, resource.Kind, chan<- []state.Event, ...state.WatchKindOption) error {
	return s.err
}

func TestCloseWaitersDoNotCancelSharedCleanup(t *testing.T) {
	r, _, finishSetup := lifecycleTestBlockedSetup(t)
	waitContext, stopWaiting := context.WithCancel(context.Background())
	defer stopWaiting()
	first := make(chan error, 1)
	go func() { first <- r.Close(waitContext) }()
	waitLifecycle(t, r, func() bool {
		return r.lifecycle.desiredState().Mode == runnerlifecycle.Closing
	}, "local shutdown intent")
	stopWaiting()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	select {
	case err := <-first:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("canceled Close waiter = %v, want context.Canceled", err)
		}
	case <-ctx.Done():
		t.Fatal("canceled Close waiter remained blocked on cleanup")
	}
	expired, releaseExpired := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer releaseExpired()
	if err := r.Close(expired); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expired concurrent Close waiter = %v, want context.DeadlineExceeded", err)
	}
	if err := r.ctx.Err(); err != nil {
		t.Fatalf("Close waiter canceled the Runner lifetime: %v", err)
	}
	owner, err := retained.OpenOwner(r.config.StateDir)
	if err == nil {
		_ = owner.Close()
		t.Fatal("canceled Close waiter released state ownership while setup was active")
	}
	finishSetup()
	if err := r.Close(ctx); err != nil {
		t.Fatalf("Close after setup finished = %v", err)
	}
	owner, err = retained.OpenOwner(r.config.StateDir)
	if err != nil {
		t.Fatalf("completed Close retained ownership: %v", err)
	}
	_ = owner.Close()
	// Completion remains observable even when a later caller's context has expired.
	if err := r.Close(expired); err != nil {
		t.Fatalf("already completed Close with expired context = %v", err)
	}
}

func TestCloseReportsExitedLifecycleEngine(t *testing.T) {
	failure := errors.New("injected lifecycle watch failure")
	store := &failedLifecycleWatchState{
		State: state.WrapCore(namespaced.NewState(inmem.Build)), err: failure,
	}
	proxy := modelConfigServer(t)
	r, err := newRunner(lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false"), nil, store)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		r.cancel()
		<-r.lifecycle.done.Done()
		r.lifecycle.workers.Wait()
		_ = r.sessions.Close()
		_ = r.owner.Close()
	})
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	select {
	case <-r.lifecycle.done.Done():
	case <-ctx.Done():
		t.Fatal("lifecycle engine did not report its watch failure")
	}
	closed := make(chan error, 1)
	go func() { closed <- r.Close(ctx) }()
	select {
	case err := <-closed:
		if !errors.Is(err, failure) {
			t.Fatalf("Close after lifecycle exit = %v, want watch failure", err)
		}
	case <-ctx.Done():
		r.cancel()
		<-closed
		t.Fatal("Close waited for an exited lifecycle engine")
	}
	if r.lifecycle.status().Stage == runnerlifecycle.Closed {
		t.Fatal("engine failure reported completed lifecycle cleanup")
	}
	owner, err := retained.OpenOwner(r.config.StateDir)
	if err == nil {
		_ = owner.Close()
		t.Fatal("Close released ownership without proving native cleanup")
	}
}

package runtime

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

type desiredPublicationState struct {
	state.State
	committed chan struct{}
	release   chan struct{}
	failure   error
	commit    bool
	once      sync.Once
	armed     atomic.Bool
}

func (s *desiredPublicationState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if value.Metadata().Type() != lifecycle.EnvironmentDesiredStateType || !s.armed.Load() {
		return s.State.Modify(ctx, value, update, opts...)
	}
	if s.commit {
		if err := s.State.Modify(ctx, value, update, opts...); err != nil {
			return err
		}
	}
	s.once.Do(func() { close(s.committed) })
	if s.release != nil {
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if s.failure != nil {
		return s.failure
	}
	if !s.commit {
		return s.State.Modify(ctx, value, update, opts...)
	}
	return nil
}

func admissionTestRuntime(t *testing.T, store state.State) *Runtime {
	t.Helper()
	desired := lifecycle.EnvironmentDesired{Mode: lifecycle.Running, IncarnationUID: "test-incarnation", Generation: 1}
	if err := lifecycle.PublishEnvironmentDesired(t.Context(), store, desired); err != nil {
		t.Fatal(err)
	}
	count := environmentAdmissionCountSpec{}
	if err := cosi.Publish(t.Context(), store, lifecycle.ResourceFor(lifecycle.ResourceID, count), count, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	return &Runtime{lifecycleContext: t.Context(), lifecycleState: store, desired: desired, admissionOpen: true, startupComplete: true, changes: make(chan struct{}), fatal: make(chan error, 1)}
}

func TestDesiredPublicationFailureRevokesNativeAdmission(t *testing.T) {
	for _, committed := range []bool{false, true} {
		name := "before-commit"
		if committed {
			name = "committed-then-error"
		}
		t.Run(name, func(t *testing.T) {
			cause := errors.New("desired publication failed")
			store := &desiredPublicationState{State: state.WrapCore(namespaced.NewState(inmem.Build)), committed: make(chan struct{}), failure: cause, commit: committed}
			r := admissionTestRuntime(t, store)
			store.armed.Store(true)
			old := r.desired
			if _, err := r.publishDesired(t.Context(), lifecycle.Quiescing, old.IncarnationUID, ""); !errors.Is(err, cause) {
				t.Fatalf("publish = %v", err)
			}
			called := false
			if err := r.admitLifecycleEffect(t.Context(), old, func(context.Context) error { called = true; return nil }); err != nil {
				t.Fatal(err)
			}
			if called {
				t.Fatal("old revision admitted native work after uncertain desired publication")
			}
			r.mu.Lock()
			open, failed := r.admissionOpen, r.failed
			revision := r.desired.Generation
			r.mu.Unlock()
			if revision != old.Generation+1 {
				t.Fatalf("failed publication reused prior revision: got %d", revision)
			}
			if open || !failed {
				t.Fatalf("publication failure: open=%t failed=%t", open, failed)
			}
		})
	}
}

func TestPublishedDesiredCannotAdmitPriorRevision(t *testing.T) {
	store := &desiredPublicationState{State: state.WrapCore(namespaced.NewState(inmem.Build)), committed: make(chan struct{}), release: make(chan struct{}), commit: true}
	r := admissionTestRuntime(t, store)
	store.armed.Store(true)
	old := r.desired
	published := make(chan error, 1)
	go func() {
		_, err := r.publishDesired(t.Context(), lifecycle.Quiescing, old.IncarnationUID, "")
		published <- err
	}()
	select {
	case <-store.committed:
	case <-t.Context().Done():
		t.Fatal(t.Context().Err())
	}
	current, found, err := lifecycle.ReadEnvironmentDesired(t.Context(), store)
	if err != nil || !found || current.Generation != old.Generation+1 {
		t.Fatalf("published desired = %+v, found=%t err=%v", current, found, err)
	}
	entered, finished := make(chan struct{}), make(chan error, 1)
	called := make(chan struct{}, 1)
	go func() {
		close(entered)
		finished <- r.admitLifecycleEffect(t.Context(), old, func(context.Context) error { called <- struct{}{}; return nil })
	}()
	<-entered
	select {
	case <-called:
		t.Fatal("old effect ran during desired publication")
	default:
	}
	close(store.release)
	if err := <-published; err != nil {
		t.Fatal(err)
	}
	if err := <-finished; err != nil {
		t.Fatal(err)
	}
	select {
	case <-called:
		t.Fatal("old effect ran after desired publication")
	default:
	}
}

func TestAcceptedNativeRegistrationRemainsInDrainAccounting(t *testing.T) {
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	r := admissionTestRuntime(t, store)
	expected := r.desired
	registered, release, finished := make(chan struct{}), make(chan struct{}), make(chan error, 1)
	go func() {
		finished <- r.admitLifecycleEffect(t.Context(), expected, func(context.Context) error {
			close(registered)
			<-release
			// Failure callbacks synchronously take Runtime.mu. Registration runs outside
			// that lock while its accounting still prevents drain completion.
			r.fail(errors.New("test component publication failed"))
			return nil
		})
	}()
	<-registered
	count, err := r.admissionCount(t.Context())
	if err != nil || count != 1 {
		t.Fatalf("accepted registration count=%d err=%v", count, err)
	}
	if _, err := r.publishDesired(t.Context(), lifecycle.Quiescing, expected.IncarnationUID, ""); err != nil {
		t.Fatal(err)
	}
	count, err = r.admissionCount(t.Context())
	if err != nil || count != 1 {
		t.Fatalf("draining registration count=%d err=%v", count, err)
	}
	close(release)
	if err := <-finished; err != nil {
		t.Fatal(err)
	}
	count, err = r.admissionCount(t.Context())
	if err != nil || count != 0 {
		t.Fatalf("settled registration count=%d err=%v", count, err)
	}
}

func TestFailedNativeRegistrationCannotReplay(t *testing.T) {
	r := admissionTestRuntime(t, state.WrapCore(namespaced.NewState(inmem.Build)))
	expected := r.desired
	failure := errors.New("registration publication outcome unknown")
	calls := 0
	effect := func(context.Context) error { calls++; return failure }
	if err := r.admitLifecycleEffect(t.Context(), expected, effect); !errors.Is(err, failure) {
		t.Fatalf("registration error=%v", err)
	}
	if err := r.admitLifecycleEffect(t.Context(), expected, effect); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("unknown registration replayed %d times", calls)
	}
	count, err := r.admissionCount(t.Context())
	if err != nil || count != 0 {
		t.Fatalf("settled registration count=%d err=%v", count, err)
	}
}

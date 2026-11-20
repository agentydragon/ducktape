package coordinator

import (
	"context"
	"errors"
	"io"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

func closeBackingOnCleanup(t *testing.T, backing io.Closer) func() error {
	t.Helper()
	closed := false
	close := func() error {
		if closed {
			return nil
		}
		closed = true
		return backing.Close()
	}
	t.Cleanup(func() {
		if err := close(); err != nil {
			t.Error(err)
		}
	})
	return close
}

func openPersistentEnvironmentState(t *testing.T, stateDir string) (state.State, *EnvironmentState, func() error) {
	t.Helper()
	lifecycleState, backing, err := cosistate.Open(stateDir, lifecycle.Namespace, cosistate.EnvironmentSealRegistration())
	if err != nil {
		t.Fatal(err)
	}
	environmentState, err := NewEnvironmentState(lifecycleState)
	if err != nil {
		_ = backing.Close()
		t.Fatal(err)
	}
	return lifecycleState, environmentState, closeBackingOnCleanup(t, backing)
}

// requireIncarnation checks the adopted incarnation UID (empty: none adopted) and lameduck state.
func requireIncarnation(t *testing.T, environmentState *EnvironmentState, want string, wantLameduck bool) {
	t.Helper()
	uid, known := environmentState.IncarnationUID()
	if uid != want || known != (want != "") || environmentState.Lameduck() != wantLameduck {
		t.Fatalf("incarnation = (%q, %t), lameduck = %t; want %q, lameduck = %t", uid, known, environmentState.Lameduck(), want, wantLameduck)
	}
}

func TestEnvironmentStatePersistsSealedUIDAcrossRestartAndSuccessorAdoption(t *testing.T) {
	stateDir := t.TempDir()
	lifecycleState, environmentState, backing := openPersistentEnvironmentState(t, stateDir)
	if _, err := environmentState.RequireIncarnationUID(); err == nil {
		t.Fatal("RequireIncarnationUID succeeded before an incarnation was adopted")
	}
	if err := environmentState.AdoptIncarnation("incarnation-one"); err != nil {
		t.Fatal(err)
	}
	if err := environmentState.EnterLameduck(); err != nil {
		t.Fatal(err)
	}
	if seal, found, err := cosistate.ReadEnvironmentSeal(context.Background(), lifecycleState); err != nil || !found || seal != (cosistate.EnvironmentSeal{IncarnationUID: "incarnation-one"}) {
		t.Fatalf("persisted seal = %+v, found=%t, err=%v", seal, found, err)
	}
	if err := backing(); err != nil {
		t.Fatal(err)
	}

	lifecycleState, environmentState, backing = openPersistentEnvironmentState(t, stateDir)
	requireIncarnation(t, environmentState, "", true)
	if uid, found, err := environmentState.SealedUID(); err != nil || !found || uid != "incarnation-one" {
		t.Fatalf("sealed UID after restart = %q, found=%t, err=%v", uid, found, err)
	}
	if err := environmentState.AdoptIncarnation("incarnation-one"); err != nil {
		t.Fatal(err)
	}
	if !environmentState.Lameduck() {
		t.Fatal("adopting the sealed incarnation reopened it")
	}
	if err := environmentState.AdoptIncarnation("incarnation-two"); err != nil {
		t.Fatal(err)
	}
	requireIncarnation(t, environmentState, "incarnation-two", false)
	if _, found, err := cosistate.ReadEnvironmentSeal(context.Background(), lifecycleState); err != nil || found {
		t.Fatalf("seal after successor adoption found=%t, err=%v; want absent", found, err)
	}
	if err := backing(); err != nil {
		t.Fatal(err)
	}

	_, environmentState, _ = openPersistentEnvironmentState(t, stateDir)
	requireIncarnation(t, environmentState, "", false)
}

type uncertainEnvironmentSealState struct {
	state.State
	fail atomic.Bool
}

func (s *uncertainEnvironmentSealState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if value.Metadata().Type() == cosistate.EnvironmentSealType && s.fail.Load() {
		if err := s.State.Modify(ctx, value, update, opts...); err != nil {
			return err
		}
		return errors.New("injected uncertain environment seal persistence outcome")
	}
	return s.State.Modify(ctx, value, update, opts...)
}

func (s *uncertainEnvironmentSealState) Destroy(ctx context.Context, pointer resource.Pointer, opts ...state.DestroyOption) error {
	if pointer.Type() == cosistate.EnvironmentSealType && s.fail.Load() {
		if err := s.State.Destroy(ctx, pointer, opts...); err != nil {
			return err
		}
		return errors.New("injected uncertain environment seal persistence outcome")
	}
	return s.State.Destroy(ctx, pointer, opts...)
}

func TestUncertainEnvironmentSealPersistenceFailsClosedUntilRestart(t *testing.T) {
	stateDir := t.TempDir()
	lifecycleState, backing, err := cosistate.Open(stateDir, lifecycle.Namespace, cosistate.EnvironmentSealRegistration())
	if err != nil {
		t.Fatal(err)
	}
	closeBacking := closeBackingOnCleanup(t, backing)
	uncertainState := &uncertainEnvironmentSealState{State: lifecycleState}
	environmentState, err := NewEnvironmentState(uncertainState)
	if err != nil {
		t.Fatal(err)
	}
	if err := environmentState.AdoptIncarnation("incarnation-one"); err != nil {
		t.Fatal(err)
	}
	uncertainState.fail.Store(true)
	if err := environmentState.EnterLameduck(); err == nil {
		t.Fatal("EnterLameduck succeeded despite a COSI persistence failure")
	}
	if !environmentState.Lameduck() {
		t.Fatal("uncertain seal write did not fail closed")
	}
	if _, _, err := environmentState.SealedUID(); err == nil {
		t.Fatal("SealedUID succeeded with an uncertain persistence outcome")
	}
	if err := environmentState.AdoptIncarnation("incarnation-two"); err == nil {
		t.Fatal("successor adoption succeeded with an uncertain persistence outcome")
	}
	if err := closeBacking(); err != nil {
		t.Fatal(err)
	}

	reopenedState, reopened, reopenedBacking := openPersistentEnvironmentState(t, stateDir)
	requireIncarnation(t, reopened, "", true)
	if uid, found, err := reopened.SealedUID(); err != nil || !found || uid != "incarnation-one" {
		t.Fatalf("sealed UID reloaded after uncertain seal write = %q, found=%t, err=%v", uid, found, err)
	}
	uncertainState = &uncertainEnvironmentSealState{State: reopenedState}
	reopened, err = NewEnvironmentState(uncertainState)
	if err != nil {
		t.Fatal(err)
	}
	if err := reopened.AdoptIncarnation("incarnation-one"); err != nil {
		t.Fatal(err)
	}
	uncertainState.fail.Store(true)
	if err := reopened.AdoptIncarnation("incarnation-two"); err == nil {
		t.Fatal("successor adoption succeeded despite an uncertain seal deletion")
	}
	if !reopened.Lameduck() {
		t.Fatal("uncertain seal deletion did not fail closed")
	}
	if _, _, err := reopened.SealedUID(); err == nil {
		t.Fatal("SealedUID succeeded after uncertain seal deletion")
	}
	if err := reopenedBacking(); err != nil {
		t.Fatal(err)
	}

	_, recovered, _ := openPersistentEnvironmentState(t, stateDir)
	requireIncarnation(t, recovered, "", false)
	if err := recovered.AdoptIncarnation("incarnation-two"); err != nil {
		t.Fatalf("adoption after restart and COSI reload: %v", err)
	}
}

func TestEnvironmentStateSerializesConcurrentSealAndSuccessorAdoption(t *testing.T) {
	lifecycleState := state.WrapCore(namespaced.NewState(inmem.Build))
	environmentState, err := NewEnvironmentState(lifecycleState)
	if err != nil {
		t.Fatal(err)
	}
	if err := environmentState.AdoptIncarnation("incarnation-one"); err != nil {
		t.Fatal(err)
	}

	start := make(chan struct{})
	var workers sync.WaitGroup
	workers.Add(2)
	errs := make(chan error, 2)
	go func() {
		defer workers.Done()
		<-start
		errs <- environmentState.EnterLameduck()
	}()
	go func() {
		defer workers.Done()
		<-start
		errs <- environmentState.AdoptIncarnation("incarnation-two")
	}()
	close(start)
	workers.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}

	uid, known := environmentState.IncarnationUID()
	if uid != "incarnation-two" || !known {
		t.Fatalf("incarnation after concurrent seal and adoption = (%q, %t)", uid, known)
	}
	persisted, found, err := cosistate.ReadEnvironmentSeal(context.Background(), lifecycleState)
	if err != nil || found != environmentState.Lameduck() || found && persisted.IncarnationUID != uid {
		t.Fatalf("persisted state = %+v, found=%t, err=%v; incarnation=%q, lameduck=%t", persisted, found, err, uid, environmentState.Lameduck())
	}
}

package runtime

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/cosi-project/runtime/pkg/state/owned"
)

func initialSessionWaitRuntime(t *testing.T) *Runtime {
	t.Helper()
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	return &Runtime{sessionWorkerDone: make(chan struct{}), changes: make(chan struct{}), lifecycleState: store, desired: lifecycle.EnvironmentDesired{Mode: lifecycle.Inactive, Generation: 1}}
}

func TestInitialSessionWaitObservesRecoveryFailureAcrossSubscription(t *testing.T) {
	for _, beforeSubscribe := range []bool{true, false} {
		name := "after-subscribe"
		if beforeSubscribe {
			name = "before-subscribe"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			failure := errors.New("Session recovery failed")
			r := initialSessionWaitRuntime(t)
			r.sessionWorkerErr = failure
			subscribed := make(chan struct{})
			var observed sync.Once
			r.deps.afterInitialWaitSnapshot = func() { observed.Do(func() { close(subscribed) }) }
			if beforeSubscribe {
				close(r.sessionWorkerDone)
			}
			result := make(chan error, 1)
			go func() { result <- r.waitForInitialSessions(ctx) }()
			select {
			case <-subscribed:
			case <-ctx.Done():
				t.Fatal("initial wait did not inspect Session recovery")
			}
			if !beforeSubscribe {
				close(r.sessionWorkerDone)
			}
			select {
			case err := <-result:
				if !errors.Is(err, failure) {
					t.Fatalf("initial Session wait = %v, want recovery error", err)
				}
			case <-ctx.Done():
				t.Fatal("Session recovery failure did not wake initial wait")
			}
		})
	}
}

func TestInitialSessionWaitRequiresRecoveryEvenWithNativeDrainComplete(t *testing.T) {
	for _, recoveryFails := range []bool{true, false} {
		name := "successful-recovery"
		if recoveryFails {
			name = "failed-recovery"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			recoveryErr := errors.New("Session recovery failed")
			r := initialSessionWaitRuntime(t)
			writer := owned.New(r.lifecycleState, "test-session-lifecycle")
			if _, err := lifecycle.WriteStatus(ctx, writer, lifecycle.Status{Generation: 1, Stage: lifecycle.Flushing, NativeStopped: true}); err != nil {
				t.Fatal(err)
			}
			if recoveryFails {
				r.sessionWorkerErr = recoveryErr
			}
			subscribed := make(chan struct{})
			var observed sync.Once
			r.deps.afterInitialWaitSnapshot = func() { observed.Do(func() { close(subscribed) }) }
			result := make(chan error, 1)
			go func() { result <- r.waitForInitialSessions(ctx) }()
			select {
			case <-subscribed:
			case <-ctx.Done():
				t.Fatal("initial wait did not inspect Session progress")
			}
			select {
			case err := <-result:
				t.Fatalf("drain status bypassed Session recovery: %v", err)
			default:
			}
			close(r.sessionWorkerDone)
			select {
			case err := <-result:
				if recoveryFails && !errors.Is(err, recoveryErr) {
					t.Fatalf("wait = %v, want recovery failure", err)
				}
				if !recoveryFails && err != nil {
					t.Fatalf("wait after Session recovery = %v", err)
				}
			case <-ctx.Done():
				t.Fatal("Session recovery did not wake initial wait")
			}
		})
	}
}

func TestInitialSessionWaitWakesOnRuntimeFailureOrClose(t *testing.T) {
	for _, closing := range []bool{false, true} {
		name := "failure"
		if closing {
			name = "close"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			failure := errors.New("injected Runtime failure")
			r := initialSessionWaitRuntime(t)
			subscribed := make(chan struct{})
			var observed sync.Once
			r.deps.afterInitialWaitSnapshot = func() { observed.Do(func() { close(subscribed) }) }
			result := make(chan error, 1)
			go func() { result <- r.waitForInitialSessions(ctx) }()
			select {
			case <-subscribed:
			case <-ctx.Done():
				t.Fatal("initial Session wait did not observe Runtime state")
			}
			r.mu.Lock()
			r.failed = !closing
			r.closing = closing
			if !closing {
				r.failureErr = failure
			}
			r.changedLocked()
			r.mu.Unlock()
			select {
			case err := <-result:
				if !closing && !errors.Is(err, failure) {
					t.Fatalf("wait after failure = %v, want injected cause", err)
				}
				if closing && (err == nil || !strings.Contains(err.Error(), "closed")) {
					t.Fatalf("wait after close = %v", err)
				}
			case <-ctx.Done():
				t.Fatalf("Runtime %s did not wake initial wait", name)
			}
		})
	}
}

func TestClosePreparationRetainsSessionCleanupAfterRecoveryFailure(t *testing.T) {
	r := &Runtime{sessions: &session.Manager{}, sessionWorkerDone: make(chan struct{}), sessionWorkerErr: errors.New("Session recovery failed")}
	close(r.sessionWorkerDone)
	coordinator := &runtimeCloseController{r: r}
	if err := coordinator.prepare(); err != nil {
		t.Fatal(err)
	}
	defer coordinator.plan.cancelFence()
	if !coordinator.plan.sessionsStarted {
		t.Fatal("Close lost ownership of Session cleanup after recovery failed")
	}
}

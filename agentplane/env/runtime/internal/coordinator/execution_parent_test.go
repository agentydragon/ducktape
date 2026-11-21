package coordinator

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
)

// Pause after startup's final check but before Runtime publishes the owner.
// Embedding the real parent keeps every other operation on its production path.
type heldExecutionPublication struct {
	executionParent
	recovery bool
	entered  chan struct{}
	release  chan struct{}
	admitted chan bool
	once     sync.Once
}

func (p *heldExecutionPublication) publishExecutionOwner(uid string, result executionStartResult, requested bool) bool {
	if result.recovery != p.recovery {
		return p.executionParent.publishExecutionOwner(uid, result, requested)
	}
	p.once.Do(func() {
		close(p.entered)
		<-p.release
	})
	admitted := p.executionParent.publishExecutionOwner(uid, result, requested)
	p.admitted <- admitted
	return admitted
}

func assertExecutionMutexAvailable(t *testing.T, h *executionLifecycle) {
	t.Helper()
	unlocked := make(chan struct{})
	go func() {
		h.mu.Lock()
		h.mu.Unlock()
		close(unlocked)
	}()
	waitSignal(t, unlocked, "timed out waiting for lifecycle mutex while parent publication is blocked")
}

func assertExecutionOwnerRetained(t *testing.T, r *Runtime, handle executionHandle) {
	t.Helper()
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.executionOwner == nil || r.executionOwner.handle != handle || r.executionOwner.admitted {
		t.Fatalf("unfenced owner was discarded or admitted: %+v", r.executionOwner)
	}
}

func TestExecutionParentStartPublicationRechecksAdmission(t *testing.T) {
	for _, revoke := range []string{"caller-cancel", "close"} {
		t.Run(revoke, func(t *testing.T) {
			p := &heldExecutionPublication{entered: make(chan struct{}), release: make(chan struct{}), admitted: make(chan bool, 1)}
			fenceEntered, releaseFence := make(chan struct{}), make(chan struct{})
			handle := newControllerExecutionHandle(func(context.Context) error {
				close(fenceEntered)
				<-releaseFence
				return nil
			})
			var starts atomic.Int32
			r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
				starts.Add(1)
				return handle, nil
			}, func(parent executionParent) executionParent { p.executionParent = parent; return p })
			var publication, fence sync.Once
			t.Cleanup(func() {
				publication.Do(func() { close(p.release) })
				fence.Do(func() { close(releaseFence) })
			})
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			startDone := make(chan error, 1)
			go func() { startDone <- r.startExecutionForUID(ctx, "incarnation") }()
			waitSignal(t, p.entered, "timed out waiting for start owner publication")
			assertExecutionMutexAvailable(t, r.executionLifecycle)
			if revoke == "caller-cancel" {
				cancel()
			} else {
				// Close revokes admission under this same aggregate mutex, before
				// it asks the lifecycle to cancel/fence the accepted startup.
				r.mu.Lock()
				r.closing = true
				r.changedLocked()
				r.mu.Unlock()
			}
			publication.Do(func() { close(p.release) })
			waitSignal(t, fenceEntered, "timed out waiting for rejected start fence")
			if <-p.admitted {
				t.Fatal("stale start was admitted at owner publication")
			}
			assertExecutionOwnerRetained(t, r, handle)
			fence.Do(func() { close(releaseFence) })
			if err := waitClose(t, startDone); err == nil || revoke == "caller-cancel" && !errors.Is(err, context.Canceled) {
				t.Fatalf("superseded start result = %v", err)
			}
			current := waitExecutionStatus(t, r, func(s executionStatus) bool {
				return s.Phase == executionStopped && s.FencedGeneration == s.Generation
			})
			if !current.AwaitDemand || starts.Load() != 1 {
				t.Fatalf("superseded demand replayed: status=%+v starts=%d", current, starts.Load())
			}
		})
	}
}

func TestExecutionParentFenceDrainsCallsBeforeReleasingOwner(t *testing.T) {
	fenceEntered, releaseFence := make(chan struct{}), make(chan struct{})
	handle := newControllerExecutionHandle(func(context.Context) error {
		close(fenceEntered)
		<-releaseFence
		return nil
	})
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) { return handle, nil })
	var release sync.Once
	t.Cleanup(func() { release.Do(func() { close(releaseFence) }) })
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	canceled := make(chan struct{})
	r.mu.Lock()
	r.activeExecutionCalls = 1
	r.execCalls[1] = func() { close(canceled) }
	r.mu.Unlock()
	t.Cleanup(func() {
		r.mu.Lock()
		r.activeExecutionCalls = 0
		delete(r.execCalls, 1)
		r.changedLocked()
		r.mu.Unlock()
	})
	stopDone := make(chan error, 1)
	go func() { stopDone <- r.stopExecution(context.Background()) }()
	waitSignal(t, canceled, "timed out waiting for forwarded call cancellation")
	assertExecutionOwnerRetained(t, r, handle)
	select {
	case <-fenceEntered:
		t.Fatal("native fencing ran before the forwarded call joined")
	default:
	}
	r.mu.Lock()
	r.activeExecutionCalls = 0
	delete(r.execCalls, 1)
	r.changedLocked()
	r.mu.Unlock()
	waitSignal(t, fenceEntered, "timed out waiting for native fence after call drain")
	assertExecutionOwnerRetained(t, r, handle)
	select {
	case err := <-stopDone:
		t.Fatalf("stop completed without fence proof: %v", err)
	default:
	}
	release.Do(func() { close(releaseFence) })
	if err := waitClose(t, stopDone); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	retained := r.executionOwner != nil
	r.mu.Unlock()
	if retained {
		t.Fatal("proved fence retained its owner")
	}
}

func TestExecutionParentRecoveryPublicationRechecksBinding(t *testing.T) {
	for _, revoke := range []string{"generation", "incarnation", "seal"} {
		t.Run(revoke, func(t *testing.T) {
			p := &heldExecutionPublication{recovery: true, entered: make(chan struct{}), release: make(chan struct{}), admitted: make(chan bool, 1)}
			first := newControllerExecutionHandle(nil)
			fenceEntered, releaseFence := make(chan struct{}), make(chan struct{})
			second := newControllerExecutionHandle(func(context.Context) error {
				close(fenceEntered)
				<-releaseFence
				return nil
			})
			var starts atomic.Int32
			r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) {
				if starts.Add(1) == 1 {
					return first, nil
				}
				return second, nil
			}, func(parent executionParent) executionParent { p.executionParent = parent; return p })
			var publication, fence sync.Once
			t.Cleanup(func() {
				publication.Do(func() { close(p.release) })
				fence.Do(func() { close(releaseFence) })
			})
			if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
				t.Fatal(err)
			}
			first.exited <- childExit{}
			waitSignal(t, p.entered, "timed out waiting for recovery owner publication")
			assertExecutionMutexAvailable(t, r.executionLifecycle)
			if revoke == "seal" {
				if err := r.state.EnterLameduck(); err != nil {
					t.Fatal(err)
				}
			} else {
				r.mu.Lock()
				if revoke == "generation" {
					r.desired.Generation++
				} else {
					r.desired.IncarnationUID = "successor"
				}
				r.changedLocked()
				r.mu.Unlock()
			}
			publication.Do(func() { close(p.release) })
			waitSignal(t, fenceEntered, "timed out waiting for stale recovery fence")
			if <-p.admitted {
				t.Fatal("recovery published an owner after its binding was revoked")
			}
			assertExecutionOwnerRetained(t, r, second)
			fence.Do(func() { close(releaseFence) })
			current := waitExecutionStatus(t, r, func(s executionStatus) bool {
				return s.Phase == executionStopped && s.FencedGeneration == 2
			})
			if !current.AwaitDemand || starts.Load() != 2 {
				t.Fatalf("revoked recovery replayed: status=%+v starts=%d", current, starts.Load())
			}
		})
	}
}

// Hold the launch worker's registered lifetime after its completion is visible.
type heldExecutionWorker struct {
	executionParent
	registered atomic.Uint32
	returned   chan struct{}
	release    chan struct{}
}

func (p *heldExecutionWorker) registerExecutionWorker() func() {
	done := p.executionParent.registerExecutionWorker()
	if p.registered.Add(1) != 1 {
		return done
	}
	return func() {
		close(p.returned)
		<-p.release
		done()
	}
}

func TestExecutionParentCloseJoinsRegisteredWorkerBeforeStoreRelease(t *testing.T) {
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	config.CodexExecServer = &codexexecserver.Config{URL: "ws://unused"}
	config.HarnessEnvironment = map[string]string{}
	r, err := newTestRuntime(t, config.Config, nil, runtimeDeps{
		startExecution: func(context.Context, Config, string) (executionHandle, error) {
			return newControllerExecutionHandle(nil), nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	p := &heldExecutionWorker{executionParent: r, returned: make(chan struct{}), release: make(chan struct{})}
	r.executionLifecycle.parent = p
	var release sync.Once
	t.Cleanup(func() {
		release.Do(func() { close(p.release) })
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		defer cancel()
		if err := r.Close(ctx); err != nil {
			t.Error(err)
		}
	})
	r.startLifecycleEngine()
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	waitSignal(t, p.returned, "timed out waiting for registered launch worker completion")
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitSignal(t, r.lifecycleDone, "timed out waiting for Close to join the engine")
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned before the registered worker joined: %v", err)
	default:
	}
	requireOwnerRetained(t, config.StateDir, "Runtime released its state store before joining its Execution worker")
	release.Do(func() { close(p.release) })
	if err := waitClose(t, closeDone); err != nil {
		t.Fatal(err)
	}
	requireOwnerReleased(t, config.StateDir, "Runtime retained its store after the worker joined")
}

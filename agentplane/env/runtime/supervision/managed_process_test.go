package supervision

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

// The backend deliberately returns a live process after its launch caller has
// canceled. Cleanup must join that late result before proving the launch fenced.
type heldLaunchBackend struct {
	entered, releaseStart, enteredStop, releaseStop chan struct{}
	stopErr                                         error
	process                                         *Process
}

func (*heldLaunchBackend) validate() error { return nil }
func (b *heldLaunchBackend) start(_ context.Context, p *Process) error {
	close(b.entered)
	<-b.releaseStart
	b.process = p
	p.mu.Lock()
	p.active = true
	p.mu.Unlock()
	return nil
}

func (b *heldLaunchBackend) stop(_ context.Context, p *Process) error {
	close(b.enteredStop)
	<-b.releaseStop
	if b.stopErr != nil {
		return b.stopErr
	}
	p.completeWith(0, nil)
	return nil
}

func TestManagedLaunchCancellationProvesOnlyCompletedFence(t *testing.T) {
	for _, failedFence := range []bool{false, true} {
		name := "fenced"
		if failedFence {
			name = "failed fence"
		}
		t.Run(name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				store := state.WrapCore(namespaced.NewState(inmem.Build))
				engine, err := runtime.NewRuntime(store, zap.L())
				if err != nil {
					t.Fatal(err)
				}
				var workers sync.WaitGroup
				host := NewHost(ctx, store, processTestNamespace, RuntimeDomain, &workers, func(err error) { t.Errorf("process observations: %v", err) })
				if err := engine.RegisterQController(host.Controller()); err != nil {
					t.Fatal(err)
				}
				engineDone := make(chan error, 1)
				go func() { engineDone <- engine.Run(ctx) }()
				defer func() { cancel(); <-engineDone; workers.Wait() }()
				backend := &heldLaunchBackend{entered: make(chan struct{}), releaseStart: make(chan struct{}), enteredStop: make(chan struct{}), releaseStop: make(chan struct{})}
				fenceErr := errors.New("process domain still populated")
				if failedFence {
					backend.stopErr = fenceErr
				}
				supervisor := &Supervisor{makeBackend: func(ProcessConfig) processBackend { return backend }}
				launchCtx, cancelLaunch := context.WithCancel(ctx)
				done := make(chan error, 1)
				go func() {
					_, err := host.Launch(launchCtx, "test", 1, supervisor, ProcessConfig{Command: []string{"late-process"}, FenceTimeout: time.Minute})
					done <- err
				}()
				<-backend.entered
				cancelLaunch()
				synctest.Wait()
				select {
				case err := <-done:
					t.Fatalf("launch returned before late start joined: %v", err)
				default:
				}
				close(backend.releaseStart)
				<-backend.enteredStop
				synctest.Wait()
				select {
				case err := <-done:
					t.Fatalf("launch returned before process fence: %v", err)
				default:
				}
				close(backend.releaseStop)
				err = <-done
				if !errors.Is(err, context.Canceled) || errors.Is(err, ErrFencedLaunch) == failedFence {
					t.Fatalf("canceled launch = %v, failed fence=%t", err, failedFence)
				}
				if failedFence {
					if !strings.Contains(err.Error(), fenceErr.Error()) {
						t.Fatalf("lost fence error: %v", err)
					}
					if !backend.process.Running() {
						t.Fatal("negative control no longer represents surviving process")
					}
					backend.process.completeWith(0, nil)
				} else if backend.process.Running() {
					t.Fatal("fence proof returned with surviving process")
				}
			})
		})
	}
}

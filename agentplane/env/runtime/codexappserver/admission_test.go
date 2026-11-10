package codexappserver

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
)

// The component subscribes to this Runtime-owned observation to retry work
// rejected during Running preparation; it never treats the observation as admission.
type admissionObservation struct{ AcceptedGeneration uint64 }

func (admissionObservation) ResourceType() resource.Type      { return lifecycle.AdmissionCountType }
func (v admissionObservation) DeepCopy() admissionObservation { return v }

func TestAcceptedGenerationWakesDemandRejectedDuringRunningPreparation(t *testing.T) {
	var accepting atomic.Bool
	var effects atomic.Int32
	rejected := make(chan struct{})
	var rejectedOnce sync.Once
	f := newFixtureWithConfiguration(t, nil, func(host *Host) {
		admit := host.deps.Admission
		host.deps.Admission = func(ctx context.Context, expected lifecycle.EnvironmentDesired, effect func(context.Context) error) error {
			if !accepting.Load() {
				rejectedOnce.Do(func() { close(rejected) })
				return nil
			}
			return admit(ctx, expected, func(ctx context.Context) error {
				effects.Add(1)
				return effect(ctx)
			})
		}
	})
	if _, err := requestCodexAppServer(f); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	select {
	case <-rejected:
	case <-ctx.Done():
		t.Fatal("published Running demand did not reach the closed admission gate")
	}
	current := codexAppServerStatus(t, f)
	if current.ObservedDemand != 0 || current.Phase != Stopped || effects.Load() != 0 {
		t.Fatalf("unaccepted demand was consumed: status=%+v effects=%d", current, effects.Load())
	}
	f.host.mu.Lock()
	registered := f.host.initializing != nil || f.host.current != nil
	f.host.mu.Unlock()
	if registered {
		t.Fatal("closed admission registered a native operation")
	}
	accepting.Store(true)
	accepted := admissionObservation{AcceptedGeneration: 1}
	if err := cosi.Publish(ctx, f.store, lifecycle.ResourceFor(lifecycle.EnvironmentResourceID, accepted), accepted, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	ready := waitCodexStatus(t, f, func(value Status) bool {
		return value.Phase == Ready && value.ObservedDemand == 1
	}, "accepted generation to wake the existing demand")
	if ready.ProcessID == "" || effects.Load() != 1 {
		t.Fatalf("accepted demand did not launch exactly once: status=%+v effects=%d", ready, effects.Load())
	}
	// Drain the process while the controller and its context are still alive.
	// Canceling the fixture first races the readers' final COSI observations.
	if err := lifecycle.PublishEnvironmentDesired(ctx, f.store, lifecycle.EnvironmentDesired{Mode: lifecycle.Closing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	waitCodexStatus(t, f, func(value Status) bool { return value.Phase == Stopped }, "app-server shutdown")
}

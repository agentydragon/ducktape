package coordinator

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/cosi-project/runtime/pkg/state/owned"
)

type uncertainFenceHandle struct{ calls atomic.Int32 }

func (*uncertainFenceHandle) Executor() executorpb.ExecutorServer {
	return &executorpb.UnimplementedExecutorServer{}
}

func (h *uncertainFenceHandle) StopAndFence(context.Context) error {
	h.calls.Add(1)
	return errors.New("uncertain fence")
}

func TestUncertainExecutionFenceIsNotRetriedForGeneration(t *testing.T) {
	handle := &uncertainFenceHandle{}
	r := newExecutionControllerRuntime(t, func(context.Context, Config, string) (executionHandle, error) { return handle, nil })
	if err := r.startExecutionForUID(t.Context(), "incarnation"); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	r.desired = lifecycle.EnvironmentDesired{Generation: 1, Mode: lifecycle.Quiescing, IncarnationUID: "incarnation"}
	r.mu.Unlock()
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	first := r.waitForExecutionFenced(ctx, 1)
	if first == nil || !strings.Contains(first.Error(), "uncertain fence") {
		t.Fatalf("fence error = %v, want uncertain fence", first)
	}
	if err := r.waitForExecutionFenced(ctx, 1); err == nil || err.Error() != first.Error() {
		t.Fatalf("joining the same generation's fence returned %v, want %v", err, first)
	}
	if got := handle.calls.Load(); got != 1 {
		t.Fatalf("fence calls = %d, want 1", got)
	}
}

func TestUnmanagedExecutionOwnerCannotClaimFence(t *testing.T) {
	handle := &uncertainFenceHandle{}
	r := &Runtime{
		desired:        lifecycle.EnvironmentDesired{Generation: 1, Mode: lifecycle.Closing},
		executionOwner: &executionLifetime{handle: handle},
		changes:        make(chan struct{}),
		fatal:          make(chan error, 1),
	}
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	fenceErr := r.waitForExecutionFenced(ctx, 1)
	if fenceErr == nil || !strings.Contains(fenceErr.Error(), "no lifecycle controller or fence proof") {
		t.Fatalf("unmanaged Execution fence = %v, want missing proof error", fenceErr)
	}
	select {
	case err := <-r.fatal:
		if !errors.Is(err, fenceErr) {
			t.Fatalf("Runtime failure = %v, want %v", err, fenceErr)
		}
	default:
		t.Fatal("fence completed before Runtime recorded the failure")
	}
	if r.executionOwner == nil || r.executionOwner.handle != handle || handle.calls.Load() != 0 {
		t.Fatal("unmanaged Execution owner was stopped or discarded")
	}
}

func TestWaitForSessionQuiescedWakesForStatusPublishedDuringRead(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	base := state.WrapCore(namespaced.NewState(inmem.Build))
	writer := owned.New(base, "test-session-lifecycle")
	if _, err := lifecycle.WriteStatus(ctx, writer, lifecycle.Status{Generation: 1, Stage: lifecycle.Flushing}); err != nil {
		t.Fatal(err)
	}
	paused := &pausedSessionStatusReadState{State: base, read: make(chan struct{}), resume: make(chan struct{})}
	defer paused.release()
	r := &Runtime{lifecycleState: paused, desired: lifecycle.EnvironmentDesired{Generation: 1}, changes: make(chan struct{})}
	result := make(chan error, 1)
	go func() { result <- r.waitSessionStage(ctx, lifecycle.Quiesced) }()
	select {
	case <-paused.read:
	case <-ctx.Done():
		t.Fatal("quiesce waiter did not read progress")
	}
	if _, err := lifecycle.WriteStatus(ctx, writer, lifecycle.Status{Generation: 1, Stage: lifecycle.Quiesced}); err != nil {
		t.Fatal(err)
	}
	// Complete notification while the waiter still holds its old read result.
	// Subscribing after that read would miss this wakeup.
	r.changed()
	paused.release()
	select {
	case err := <-result:
		if err != nil {
			t.Fatalf("wait for Session quiesce: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("final status did not wake waiter")
	}
}

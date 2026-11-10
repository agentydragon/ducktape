package runtime

import (
	"context"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/cosi-project/runtime/pkg/state/owned"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type pausedSessionStatusReadState struct {
	state.State
	read, resume     chan struct{}
	once, resumeOnce sync.Once
}

func (s *pausedSessionStatusReadState) release() { s.resumeOnce.Do(func() { close(s.resume) }) }
func (s *pausedSessionStatusReadState) Get(ctx context.Context, pointer resource.Pointer, opts ...state.GetOption) (resource.Resource, error) {
	value, err := s.State.Get(ctx, pointer, opts...)
	if pointer.Type() == lifecycle.AggregateStatusType {
		s.once.Do(func() {
			close(s.read)
			select {
			case <-s.resume:
			case <-ctx.Done():
			}
		})
	}
	return value, err
}

func TestNativeStopWaitDoesNotMissStatusPublishedDuringRead(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	base := state.WrapCore(namespaced.NewState(inmem.Build))
	paused := &pausedSessionStatusReadState{State: base, read: make(chan struct{}), resume: make(chan struct{})}
	defer paused.release()
	r := &Runtime{desired: lifecycle.EnvironmentDesired{Generation: 3}, lifecycleState: paused, changes: make(chan struct{})}
	result := make(chan error, 1)
	go func() { result <- r.waitForNativeStopped(ctx, 3) }()
	select {
	case <-paused.read:
	case <-ctx.Done():
		t.Fatal("native-stop waiter did not read status")
	}
	writer := owned.New(base, "test-session-lifecycle")
	if _, err := lifecycle.WriteStatus(ctx, writer, lifecycle.Status{Generation: 3, NativeStopped: true}); err != nil {
		t.Fatal(err)
	}
	r.changed()
	paused.release()
	select {
	case err := <-result:
		if err != nil {
			t.Fatalf("native-stop wait: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("status update was lost between read and wait")
	}
}

func TestNativeStopWaitRejectsStaleGeneration(t *testing.T) {
	base := state.WrapCore(namespaced.NewState(inmem.Build))
	writer := owned.New(base, "test-session-lifecycle")
	if _, err := lifecycle.WriteStatus(t.Context(), writer, lifecycle.Status{Generation: 2, NativeStopped: true}); err != nil {
		t.Fatal(err)
	}
	r := &Runtime{desired: lifecycle.EnvironmentDesired{Generation: 3}, lifecycleState: base, changes: make(chan struct{})}
	if err := r.waitForNativeStopped(t.Context(), 2); status.Code(err) != codes.Aborted {
		t.Fatalf("superseded generation accepted stale native-stop status: %v", err)
	}
}

func TestNativeStopWaitRejectsGenerationSupersededDuringStatusRead(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	base := state.WrapCore(namespaced.NewState(inmem.Build))
	writer := owned.New(base, "test-session-lifecycle")
	if _, err := lifecycle.WriteStatus(ctx, writer, lifecycle.Status{Generation: 2, NativeStopped: true}); err != nil {
		t.Fatal(err)
	}
	paused := &pausedSessionStatusReadState{State: base, read: make(chan struct{}), resume: make(chan struct{})}
	defer paused.release()
	r := &Runtime{lifecycleState: paused, desired: lifecycle.EnvironmentDesired{Generation: 2}, changes: make(chan struct{})}
	result := make(chan error, 1)
	go func() { result <- r.waitForNativeStopped(ctx, 2) }()
	select {
	case <-paused.read:
	case <-ctx.Done():
		t.Fatal("native-stop waiter did not read status")
	}
	r.mu.Lock()
	r.desired.Generation = 3
	r.changedLocked()
	r.mu.Unlock()
	paused.release()
	select {
	case err := <-result:
		if status.Code(err) != codes.Aborted {
			t.Fatalf("superseded native-stop wait=%v", err)
		}
	case <-ctx.Done():
		t.Fatal("superseded generation did not abort waiter")
	}
}

func TestExecutionReadinessFollowsOwnerLifetime(t *testing.T) {
	r := &Runtime{config: Config{CodexExecServer: &codexexecserver.Config{}}, desired: lifecycle.EnvironmentDesired{IncarnationUID: "incarnation"}}
	ready, err := r.executionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness before Execution owner = %t, %v", ready, err)
	}
	handle := newControllerExecutionHandle(nil)
	r.executionOwner = &executionLifetime{handle: handle, uid: "incarnation", admitted: true}
	ready, err = r.executionReady(t.Context())
	if err != nil || !ready {
		t.Fatalf("readiness with active Execution owner = %t, %v", ready, err)
	}
	handle.mu.Lock()
	handle.expected = true
	handle.mu.Unlock()
	ready, err = r.executionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness after owner stop = %t, %v", ready, err)
	}
	handle.mu.Lock()
	handle.expected = false
	handle.mu.Unlock()
	r.executionOwner.admitted = false
	ready, err = r.executionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness during fenced recovery = %t, %v", ready, err)
	}
	backendLife, backendLost := context.WithCancel(t.Context())
	service := &executionServiceHandle{executor: &executorpb.UnimplementedExecutorServer{}, done: make(chan struct{}), backendLife: backendLife}
	r.executionOwner = &executionLifetime{handle: service, uid: "incarnation", admitted: true}
	ready, err = r.executionReady(t.Context())
	if err != nil || !ready {
		t.Fatalf("readiness while service is running = %t, %v", ready, err)
	}
	backendLost()
	ready, err = r.executionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness after backend loss = %t, %v", ready, err)
	}
	close(service.done)
	ready, err = r.executionReady(t.Context())
	if err != nil || ready {
		t.Fatalf("readiness after backend exit = %t, %v", ready, err)
	}
}

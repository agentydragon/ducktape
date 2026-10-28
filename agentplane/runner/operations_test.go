package runner

import (
	"context"
	"sync"
	"testing"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
)

// Hold the final state write open to exercise consumption during publication.
type blockedCompletionState struct {
	state.State
	publishing chan struct{}
	release    chan struct{}
}

func (s *blockedCompletionState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if result, ok := value.(*typed.Resource[operationResultSpec, lifecycleExtension[operationResultSpec]]); ok && result.TypedSpec().Done {
		close(s.publishing)
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return s.State.Modify(ctx, value, update, opts...)
}

func TestOperationCompletionPublicationPrecedesConsumption(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	r := &Runner{ctx: ctx, changes: make(chan struct{}), fatal: make(chan error, 1)}
	l, err := newLifecycle(r)
	if err != nil {
		t.Fatal(err)
	}
	st := &blockedCompletionState{State: l.state, publishing: make(chan struct{}), release: make(chan struct{})}
	l.state = st
	var mu sync.Mutex
	var slot *operation
	l.startHostOperation(&mu, &slot, "test-controller", func(context.Context) error { return nil })
	<-st.publishing
	// The native effect returned, but its completion has not been published.
	if operationDone(slot) {
		t.Error("operation became consumable before its completion was published")
	}
	// Release the state write before acquiring the host mutex, which serializes
	// publication with consumption. No host snapshot publication lock is needed.
	close(st.release)
	l.workers.Wait()
	op, done := l.takeHostOperation(&mu, &slot)
	if !done || op.err != nil || slot != nil {
		t.Fatalf("consumed operation = %+v, done=%t slot=%p", op, done, slot)
	}
	if _, found, err := lifecycleRead[operationResultSpec](ctx, l.state, op.id); err != nil || found {
		t.Fatalf("operation result survived consumption: found=%t error=%v", found, err)
	}
}

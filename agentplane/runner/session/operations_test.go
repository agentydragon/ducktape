package session

import (
	"context"
	"testing"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

// Hold the final state write open to exercise consumption during publication.
type blockedCompletionState struct {
	state.State
	publishing chan struct{}
	release    chan struct{}
}

func (s *blockedCompletionState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if result, ok := value.(*typed.Resource[operationResult, componentExtension[operationResult]]); ok && result.TypedSpec().Done {
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
	st := &blockedCompletionState{State: state.WrapCore(namespaced.NewState(inmem.Build)), publishing: make(chan struct{}), release: make(chan struct{})}
	m := &Manager{ctx: ctx, state: st, fail: func(err error) {
		if err != nil {
			t.Error(err)
		}
	}}
	s := &Session{id: "session", deps: sessionDependencies{ctx: ctx, publishOperation: m.publishOperation, publish: func(*Session, bool) {}, changed: func() {}}}
	s.startOperation(&s.preparation, func(context.Context) error { return nil })
	s.mu.Lock()
	slot := s.preparation
	s.mu.Unlock()
	<-st.publishing
	// The native effect returned, but its completion has not been published.
	if operationDone(slot) {
		t.Error("operation became consumable before its completion was published")
	}
	// Release the state write before acquiring the host mutex, which serializes
	// publication with consumption. No host snapshot publication lock is needed.
	close(st.release)
	s.work.Wait()
	op, done := s.takeOperation(&s.preparation)
	m.forgetOperation(op)
	if !done || op.err != nil || s.preparation != nil {
		t.Fatalf("consumed operation = %+v, done=%t slot=%p", op, done, s.preparation)
	}
	if _, found, err := componentRead[operationResult](ctx, st, op.id); err != nil || found {
		t.Fatalf("operation result survived consumption: found=%t error=%v", found, err)
	}
}

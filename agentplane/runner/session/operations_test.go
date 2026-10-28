package session

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/operation"
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
	s := &Session{id: "session", journal: openTestJournal(t), deps: sessionDependencies{ctx: ctx, publishOperation: m.publishOperation, publish: func(*Session, bool) {}, changed: func() {}}}
	s.startOperation(&s.preparation, func(context.Context) error { return nil })
	s.mu.Lock()
	slot := s.preparation
	s.mu.Unlock()
	<-st.publishing
	// The native effect returned, but its completion has not been published.
	if slot.Finished() {
		t.Error("operation became consumable before its completion was published")
	}
	type consumeResult struct {
		op   *operation.Operation
		done bool
	}
	consumed := make(chan consumeResult, 1)
	go func() {
		op, done := s.takeOperation(&s.preparation)
		consumed <- consumeResult{op: op, done: done}
	}()
	select {
	case result := <-consumed:
		t.Fatalf("controller consumed operation before completion publication: %+v", result)
	case <-time.After(50 * time.Millisecond):
	}
	// A controller awakened by the COSI write waits on the same owner mutex as
	// publication and local completion, then consumes without another input wake.
	close(st.release)
	var result consumeResult
	select {
	case result = <-consumed:
	case <-time.After(time.Second):
		t.Fatal("controller did not consume after completion publication")
	}
	s.work.Wait()
	m.forgetOperation(result.op)
	s.mu.Lock()
	remaining := s.preparation
	s.mu.Unlock()
	if !result.done || result.op == nil || result.op.Err() != nil || remaining != nil {
		t.Fatalf("consumed operation = %+v, done=%t slot=%p", result.op, result.done, remaining)
	}
	if _, found, err := componentRead[operationResult](ctx, st, result.op.ID()); err != nil || found {
		t.Fatalf("operation result survived consumption: found=%t error=%v", found, err)
	}
}

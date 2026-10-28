package runner

import (
	"context"
	"sync"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
)

type stopReason uint8

const (
	stopNone stopReason = iota
	stopStartupFailure
	stopClose
	stopQuiesce
)

// done closes only after err is installed under the host mutex. Slots remain owned
// until the controller consumes the result, preventing duplicate native effects.
type sessionOperation struct {
	id     string
	cancel context.CancelFunc
	done   chan struct{}
	err    error
}

func operationDone(op *sessionOperation) bool {
	if op == nil {
		return false
	}
	select {
	case <-op.done:
		return true
	default:
		return false
	}
}

func (l *lifecycle) operationResult(session, kind string, op *sessionOperation, done bool) {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	value := operationResultSpec{Session: session, Kind: kind, Done: done}
	if done && op.err != nil {
		value.Error = op.err.Error()
	}
	l.runner.fail(publishLifecycle(l, operationResultType, op.id, value))
}

func (l *lifecycle) forgetOperation(op *sessionOperation) {
	if op == nil {
		return
	}
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	err := l.state.Destroy(l.runner.ctx, resource.NewMetadata(lifecycleNamespace, operationResultType, op.id, resource.VersionUndefined), state.WithDestroyOwner("runner-host"))
	if !state.IsNotFoundError(err) {
		l.runner.fail(err)
	}
}

func (s *Session) startOperation(slot **sessionOperation, kind string, work func(context.Context) error) {
	s.mu.Lock()
	if *slot != nil {
		s.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(s.runner.ctx)
	op := &sessionOperation{id: uuid.NewString(), cancel: cancel, done: make(chan struct{})}
	*slot = op
	s.work.Add(1)
	s.mu.Unlock()
	s.runner.lifecycle.operationResult(s.id, kind, op, false)
	s.wake()
	go func() {
		defer s.work.Done()
		defer cancel()
		err := work(ctx)
		s.mu.Lock()
		op.err = err
		close(op.done)
		s.mu.Unlock()
		s.runner.lifecycle.operationResult(s.id, kind, op, true)
		s.wake()
		s.runner.changed()
	}()
}

func (s *Session) takeOperation(slot **sessionOperation) (*sessionOperation, bool) {
	s.mu.Lock()
	if !operationDone(*slot) {
		s.mu.Unlock()
		return nil, false
	}
	op := *slot
	*slot = nil
	s.mu.Unlock()
	s.wake()
	return op, true
}

// Controllers claim a slot before returning. Native effects outlive individual
// reconciliation calls, and their completion is an input to the owning controller.
func (l *lifecycle) startHostOperation(mu *sync.Mutex, slot **sessionOperation, kind string, work func(context.Context) error) {
	mu.Lock()
	if *slot != nil {
		mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(l.runner.ctx)
	op := &sessionOperation{id: uuid.NewString(), cancel: cancel, done: make(chan struct{})}
	*slot = op
	l.workers.Add(1)
	mu.Unlock()
	l.operationResult("", kind, op, false)
	go func() {
		defer l.workers.Done()
		defer cancel()
		err := work(ctx)
		mu.Lock()
		op.err = err
		close(op.done)
		mu.Unlock()
		l.operationResult("", kind, op, true)
		l.runner.changed()
	}()
}

func (l *lifecycle) takeHostOperation(mu *sync.Mutex, slot **sessionOperation) (*sessionOperation, bool) {
	mu.Lock()
	if !operationDone(*slot) {
		mu.Unlock()
		return nil, false
	}
	op := *slot
	*slot = nil
	mu.Unlock()
	l.forgetOperation(op)
	return op, true
}

// Subscribe before checking so a completion cannot be lost between the state
// read and the wait. This waits for observations; it never schedules effects.
func (l *lifecycle) waitFor(ctx context.Context, predicate func() (bool, error)) error {
	for {
		changed := l.runner.changesSince()
		ready, err := predicate()
		if ready || err != nil {
			return err
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		case <-l.runner.ctx.Done():
			return l.runner.ctx.Err()
		}
	}
}

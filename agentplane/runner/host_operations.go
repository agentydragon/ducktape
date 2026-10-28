package runner

import (
	"context"
	"sync"

	"github.com/google/uuid"
)

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

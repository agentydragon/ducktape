// Package operation owns a host's asynchronous one-shot operation lifecycle.
package operation

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/google/uuid"
)

// Operation remains in its owner's slot until its completion result has been
// published and the owner consumes it.
type Operation struct {
	id      string
	ctx     context.Context
	cancel  context.CancelFunc
	done    chan struct{}
	workers *sync.WaitGroup

	mu         sync.RWMutex
	err        error
	registered bool
	started    bool
}

func New(parent context.Context) *Operation {
	ctx, cancel := context.WithCancel(parent)
	return &Operation{id: uuid.NewString(), ctx: ctx, cancel: cancel, done: make(chan struct{})}
}

func (op *Operation) ID() string { return op.id }

func (op *Operation) Done() <-chan struct{} { return op.done }

func (op *Operation) Err() error {
	op.mu.RLock()
	defer op.mu.RUnlock()
	return op.err
}

func (op *Operation) Cancel() { op.cancel() }

// Register accounts this operation in its host's worker barrier. Owners call
// it while holding the mutex that protects the operation slot.
func (op *Operation) Register(workers *sync.WaitGroup) {
	op.mu.Lock()
	defer op.mu.Unlock()
	if op.registered || op.started {
		panic("operation worker already registered")
	}
	workers.Add(1)
	op.workers = workers
	op.registered = true
}

func (op *Operation) Finished() bool {
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

// Start publishes an in-progress result before starting work. It reports
// publication failures through report; completion publication happens before
// Done closes, and its error is retained alongside any work error.
func (op *Operation) Start(
	publish func(done bool) error,
	report func(error),
	work func(context.Context) error,
	onStart func(),
	onDone func(),
) error {
	op.mu.Lock()
	if op.started {
		op.mu.Unlock()
		return errors.New("operation already started")
	}
	if !op.registered {
		op.mu.Unlock()
		return errors.New("operation worker is not registered")
	}
	op.started = true
	workers := op.workers
	op.mu.Unlock()
	if err := publish(false); err != nil {
		failure := fmt.Errorf("publish operation start: %w", err)
		op.Cancel()
		op.finish(failure)
		report(failure)
		workers.Done()
		return failure
	}
	if onStart != nil {
		onStart()
	}
	go func() {
		defer workers.Done()
		defer op.Cancel()
		var publicationErr error
		workErr := work(op.ctx)
		if err := publish(true); err != nil {
			publicationErr = fmt.Errorf("publish operation completion: %w", err)
			workErr = errors.Join(workErr, publicationErr)
		}
		op.finish(workErr)
		if onDone != nil {
			onDone()
		}
		if publicationErr != nil {
			report(publicationErr)
		}
	}()
	return nil
}

func (op *Operation) finish(err error) {
	op.mu.Lock()
	op.err = err
	close(op.done)
	op.mu.Unlock()
}

// Consume releases a completed operation slot exactly once. The caller uses
// the same mutex for the slot and its other host state.
func Consume(mu *sync.Mutex, slot **Operation) (*Operation, bool) {
	mu.Lock()
	defer mu.Unlock()
	op := *slot
	if !op.Finished() {
		return nil, false
	}
	*slot = nil
	return op, true
}

package session

import (
	"context"
	"sync"

	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
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
type operation struct {
	id     string
	cancel context.CancelFunc
	done   chan struct{}
	err    error
}

func operationDone(op *operation) bool {
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

func (m *Manager) publishOperation(id string, op *operation, done bool) {
	m.fail(publish(m.ctx, m.state, op.id, operationResult{Session: id, Controller: ControllerName, Done: done}))
}

func (m *Manager) forgetOperation(op *operation) {
	if op == nil {
		return
	}
	err := m.state.Destroy(m.ctx, resource.NewMetadata(runnerlifecycle.Namespace, OperationType, op.id, resource.VersionUndefined), state.WithDestroyOwner(HostOwner))
	if !state.IsNotFoundError(err) {
		m.fail(err)
	}
}

// operationHost bridges bounded native effects into COSI without retrying them.
// A claimed slot remains owned until its controller consumes the completion.
type operationHost struct {
	ctx     context.Context
	mu      *sync.Mutex
	workers *sync.WaitGroup
	publish func(*operation, bool)
	refresh func()
	changed func()
}

func (h operationHost) start(slot **operation, work func(context.Context) error) {
	h.mu.Lock()
	if *slot != nil {
		h.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(h.ctx)
	op := &operation{id: uuid.NewString(), cancel: cancel, done: make(chan struct{})}
	*slot = op
	h.workers.Add(1)
	h.mu.Unlock()
	h.publish(op, false)
	if h.refresh != nil {
		h.refresh()
	}
	go func() {
		defer h.workers.Done()
		defer cancel()
		err := work(ctx)
		h.mu.Lock()
		op.err = err
		// Publish before making the slot consumable, so a late completion cannot
		// recreate a result that its controller has already removed.
		h.publish(op, true)
		close(op.done)
		h.mu.Unlock()
		if h.refresh != nil {
			h.refresh()
		}
		h.changed()
	}()
}

func takeOperation(mu *sync.Mutex, slot **operation) (*operation, bool) {
	mu.Lock()
	defer mu.Unlock()
	if !operationDone(*slot) {
		return nil, false
	}
	op := *slot
	*slot = nil
	return op, true
}

func (s *Session) startOperation(slot **operation, work func(context.Context) error) {
	operationHost{
		ctx: s.deps.ctx, mu: &s.mu, workers: &s.work,
		publish: func(op *operation, done bool) { s.deps.publishOperation(s.id, op, done) },
		refresh: s.wake, changed: s.deps.changed,
	}.start(slot, work)
}

func (s *Session) takeOperation(slot **operation) (*operation, bool) {
	op, done := takeOperation(&s.mu, slot)
	if done {
		s.wake()
	}
	return op, done
}

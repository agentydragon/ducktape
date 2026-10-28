package session

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/operation"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
)

type stopReason uint8

const (
	stopNone stopReason = iota
	stopStartupFailure
	stopClose
	stopQuiesce
)

func (m *Manager) publishOperation(sessionID, operationID string, done bool) error {
	return publish(m.ctx, m.state, operationID, operationResult{Session: sessionID, Controller: ControllerName, Done: done})
}

func (m *Manager) forgetOperation(op *operation.Operation) {
	if op == nil {
		return
	}
	err := m.state.Destroy(m.ctx, resource.NewMetadata(lifecycle.Namespace, OperationType, op.ID(), resource.VersionUndefined), state.WithDestroyOwner(HostOwner))
	if !state.IsNotFoundError(err) {
		m.fail(err)
	}
}

func (s *Session) startOperation(slot **operation.Operation, work func(context.Context) error) {
	s.mu.Lock()
	if *slot != nil {
		s.mu.Unlock()
		return
	}
	op := operation.New(s.deps.ctx)
	op.Register(&s.work)
	*slot = op
	s.mu.Unlock()

	err := op.Start(
		&s.mu,
		func(done bool) error { return s.deps.publishOperation(s.id, op.ID(), done) },
		s.deps.fail,
		work,
		s.wake,
		func() {
			s.wake()
			s.deps.changed()
		},
	)
	if err != nil {
		s.mu.Lock()
		if *slot == op {
			*slot = nil
		}
		s.mu.Unlock()
	}
}

func (s *Session) takeOperation(slot **operation.Operation) (*operation.Operation, bool) {
	op, done := operation.Consume(&s.mu, slot)
	if done {
		s.wake()
	}
	return op, done
}

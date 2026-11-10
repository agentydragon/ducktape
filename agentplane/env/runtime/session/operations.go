package session

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/oneoff"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

type stopReason uint8

const (
	stopNone stopReason = iota
	stopStartupFailure
	stopClose
	stopQuiesce
)

func (m *Manager) signalOperation(sessionID string) error {
	// Completion holds the Session mutex. Do not take publishMu here: snapshot
	// publication holds publishMu while reading Session fields under that mutex.
	return safe.StateModify(m.ctx, m.state, lifecycle.ResourceFor(sessionID, ReconcileSnapshot{}), func(current *typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]]) error {
		current.TypedSpec().OperationRevision++
		return nil
	}, state.WithUpdateOwner(lifecycle.EnvironmentOwner))
}

func (s *Session) startOperation(slot **oneoff.Operation, work func(context.Context) error) (*oneoff.Operation, error) {
	s.mu.Lock()
	if *slot != nil {
		op := *slot
		s.mu.Unlock()
		return op, nil
	}
	op := oneoff.New(s.deps.ctx)
	op.Register(&s.work)
	*slot = op
	s.mu.Unlock()

	err := op.Start(
		&s.mu,
		func(bool) error { return s.deps.signalOperation(s.id) },
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
		s.operationStartFailed = true
		s.mu.Unlock()
	}
	return op, err
}

func (s *Session) takeOperation(slot **oneoff.Operation) (*oneoff.Operation, bool) {
	op, done := oneoff.Consume(&s.mu, slot)
	if done {
		s.wake()
	}
	return op, done
}

package runner

import (
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

func (l *lifecycle) publishSeal(s *Session) error {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	value := journalSealSpec{}
	if seal := s.journal.SealState(); seal != nil {
		value = journalSealSpec{Present: true, IncarnationUID: seal.IncarnationUID, Cursor: seal.Cursor}
	}
	return publishLifecycle(l, s.id, value)
}

func (l *lifecycle) publishAck(id string, ack retained.Acknowledgement) error {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	return publishLifecycle(l, id, ingressAckSpec{Cursor: ack.Cursor, Sealed: ack.Sealed})
}

// Host publishers serialize compound snapshots; COSI handles conflicts and no-op updates.
func publishLifecycle[T lifecycleSpec[T]](l *lifecycle, id string, value T) error {
	return safe.StateModify(l.runner.ctx, l.state, lifecycleResourceFor(id, value), func(r *typed.Resource[T, lifecycleExtension[T]]) error {
		*r.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner("runner-host"))
}

func (l *lifecycle) touchRunner() {
	l.runner.mu.Lock()
	admissions := l.runner.admissions
	l.runner.mu.Unlock()
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, _, err := lifecycleRead[runnerInputSpec](l.runner.ctx, l.state, "runner")
	if err != nil {
		l.runner.fail(err)
		return
	}
	input.Admissions = admissions
	input.Revision++
	l.runner.fail(publishLifecycle(l, "runner", input))
}

func (l *lifecycle) publishSession(s *Session, demand bool) {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, _, err := lifecycleRead[sessionInputSpec](l.runner.ctx, l.state, s.id)
	if err != nil {
		l.runner.fail(err)
		return
	}
	if demand {
		input.Demand++
	}
	s.mu.Lock()
	input.SetupRequested = s.setupScript != nil
	input.Setup, input.ActiveTurn, input.Pending, input.ProcessID = s.setup, s.activeTurn, len(s.pending), s.processID
	input.ProcessRunning = s.process != nil && s.process.Running()
	input.InspectionRunning = s.inspection != nil
	input.Inspections = len(s.reconciliations)
	input.InspectionReady = len(s.reconciliations) > 0 && s.reconciliations[0].ready
	s.mu.Unlock()
	input.Cursor = s.journal.LastCursor()
	l.runner.fail(publishLifecycle(l, s.id, input))
}
func (s *Session) wake()             { s.runner.lifecycle.publishSession(s, false) }
func (s *Session) requestExecution() { s.runner.lifecycle.publishSession(s, true) }
func (s *Session) runtimeState() sessionRuntimeSpec {
	value, _, err := lifecycleRead[sessionRuntimeSpec](s.runner.ctx, s.runner.lifecycle.state, s.id)
	if err != nil {
		s.runner.fail(err)
	}
	return value
}

func (s *Session) readyLocked() bool {
	current := s.runtimeState()
	return !operationDone(s.halting) && (current.Phase == executionReady || current.Phase == executionStopping) && current.ProcessID == s.processID && s.process != nil && s.process.Running()
}

func (s *Session) executionClosed() bool {
	return !s.runner.lifecycle.accepting() || s.journal.SealState() != nil
}

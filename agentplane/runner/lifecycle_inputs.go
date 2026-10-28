package runner

import (
	"github.com/cosi-project/runtime/pkg/state"
)

func (l *lifecycle) publishSeal(s *Session) error {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	value := journalSealSpec{}
	if seal := s.journal.SealState(); seal != nil {
		value = journalSealSpec{Present: true, IncarnationUID: seal.IncarnationUID, Cursor: seal.Cursor}
	}
	return publishLifecycle(l, journalSealType, s.id, value)
}

func (l *lifecycle) publishAck(id string, ack IngressAcknowledgement) error {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	return publishLifecycle(l, ingressAckType, id, ingressAckSpec{Cursor: ack.Cursor, Sealed: ack.Sealed})
}

// Host publishers serialize read/modify/write; COSI controllers own different outputs.
func publishLifecycle[T comparable](l *lifecycle, kind, id string, value T) error {
	r := lifecycleResourceFor(kind, id, value)
	old, err := l.state.Get(l.runner.ctx, r.Metadata())
	if state.IsNotFoundError(err) {
		return l.state.Create(l.runner.ctx, r, state.WithCreateOwner("runner-host"))
	}
	if err != nil {
		return err
	}
	if old.(*lifecycleResource[T]).value == value {
		return nil
	}
	r.metadata = old.Metadata().Copy()
	return l.state.Update(l.runner.ctx, r, state.WithUpdateOwner("runner-host"))
}

func (l *lifecycle) touchRunner() {
	l.runner.mu.Lock()
	admissions := l.runner.admissions
	l.runner.mu.Unlock()
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, _, err := lifecycleRead[runnerInputSpec](l.runner.ctx, l.state, runnerInputType, "runner")
	if err != nil {
		l.runner.fail(err)
		return
	}
	input.Admissions = admissions
	input.Revision++
	l.runner.fail(publishLifecycle(l, runnerInputType, "runner", input))
}

func (l *lifecycle) publishSession(s *Session, demand bool) {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, _, err := lifecycleRead[sessionInputSpec](l.runner.ctx, l.state, sessionInputType, s.id)
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
	l.runner.fail(publishLifecycle(l, sessionInputType, s.id, input))
}
func (s *Session) wake()             { s.runner.lifecycle.publishSession(s, false) }
func (s *Session) requestExecution() { s.runner.lifecycle.publishSession(s, true) }
func (s *Session) runtimeState() sessionRuntimeSpec {
	value, _, err := lifecycleRead[sessionRuntimeSpec](s.runner.ctx, s.runner.lifecycle.state, sessionRuntimeType, s.id)
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

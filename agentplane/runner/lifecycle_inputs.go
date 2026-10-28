package runner

import (
	"context"
	"fmt"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/cosi-project/runtime/pkg/resource"
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

func (l *lifecycle) publishAdmissionState() {
	l.runner.mu.Lock()
	admissions := l.runner.admissions
	defer l.runner.mu.Unlock()
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
	input.Harness = s.record.Harness
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

func (s *Session) wake() {
	s.mu.Lock()
	loading := s.loading
	s.mu.Unlock()
	if !loading {
		s.deps.publish(s, false)
	}
}
func (s *Session) requestExecution() { s.deps.publish(s, true) }
func (s *Session) runtimeState() sessionRuntimeSpec {
	value, _, err := lifecycleRead[sessionRuntimeSpec](s.deps.ctx, s.deps.state, s.id)
	if err != nil {
		s.deps.fail(err)
	}
	return value
}

func (s *Session) readyLocked() bool {
	current := s.runtimeState()
	return !operationDone(s.halting) && (current.Phase == executionReady || current.Phase == executionStopping) && current.ProcessID == s.processID && s.process != nil && s.process.Running()
}

func (s *Session) executionClosed() bool {
	desired, _, err := runnerlifecycle.ReadDesired(s.deps.ctx, s.deps.state)
	if err != nil {
		s.deps.fail(err)
		return true
	}
	return desired.Mode != runnerlifecycle.Running || s.deps.store.Lameduck() || s.journal.SealState() != nil
}

func (s *Session) waitSetup(ctx context.Context) error {
	s.mu.Lock()
	pending := s.setup == runnerpb.SetupState_SETUP_STATE_RUNNING
	s.mu.Unlock()
	if !pending {
		return nil
	}
	_, err := safe.StateWatchFor[*typed.Resource[sessionInputSpec, lifecycleExtension[sessionInputSpec]]](
		ctx, s.deps.state, lifecycleResourceFor(s.id, sessionInputSpec{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			input, ok := value.(*typed.Resource[sessionInputSpec, lifecycleExtension[sessionInputSpec]])
			if !ok {
				return false, fmt.Errorf("unexpected Session input resource %T", value)
			}
			return input.TypedSpec().Setup != runnerpb.SetupState_SETUP_STATE_RUNNING, nil
		}),
	)
	return err
}

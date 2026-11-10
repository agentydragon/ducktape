package session

import (
	"context"
	"fmt"

	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

func (m *Manager) publishSession(s *Session, demand bool) {
	m.fail(m.publishSessionState(s, demand))
}

func (m *Manager) publishSessionState(s *Session, demand bool) error {
	m.publishMu.Lock()
	defer m.publishMu.Unlock()
	var input ReconcileSnapshot
	s.mu.Lock()
	input.Harness = s.record.GetPlacement().GetHarness()
	input.SetupRequested = retained.HasSetupScript(s.record)
	input.Setup, input.Pending, input.ProcessID = s.setup, len(s.pending), s.processID
	input.ProcessRunning = s.process != nil && s.process.Running()
	input.InspectionRunning = s.inspection != nil
	input.Inspections = len(s.reconciliations)
	input.InspectionReady = len(s.reconciliations) > 0 && s.reconciliations[0].ready
	adapter := s.adapter
	s.mu.Unlock()
	input.DispatchBlocked = adapter == nil || adapter.BlocksCommandDispatch()
	// COSI retries this updater on conflict, so each demand increment starts from the current spec.
	if err := safe.StateModify(m.ctx, m.state, lifecycle.ResourceFor(s.id, ReconcileSnapshot{}), func(current *typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]]) error {
		next := input
		next.Demand = current.TypedSpec().Demand
		next.OperationRevision = current.TypedSpec().OperationRevision
		if demand {
			next.Demand++
		}
		*current.TypedSpec() = next
		return nil
	}, state.WithUpdateOwner(lifecycle.EnvironmentOwner)); err != nil {
		return err
	}
	return nil
}

func (s *Session) wake() {
	s.mu.Lock()
	loading := s.loading
	s.mu.Unlock()
	if !loading {
		s.notifyBlockers()
		s.deps.publish(s, false)
	}
}
func (s *Session) requestExecution() { s.deps.publish(s, true) }
func (s *Session) lifecycleStatus() LifecycleStatus {
	value, _, err := lifecycle.Read[LifecycleStatus](s.deps.ctx, s.deps.state, s.id)
	if err != nil {
		s.deps.fail(err)
	}
	return value
}

func (s *Session) readyLocked() bool {
	current := s.lifecycleStatus()
	return !s.halting.Finished() && (current.Phase == Ready || current.Phase == Stopping) && current.ProcessID == s.processID && s.process != nil && s.process.Running()
}

func (s *Session) executionClosed() bool {
	desired, _, err := lifecycle.ReadEnvironmentDesired(s.deps.ctx, s.deps.state)
	if err != nil {
		s.deps.fail(err)
		return true
	}
	return desired.Mode != lifecycle.Running || s.deps.environmentState().Quiesced || s.journal.SealState() != nil
}

func (s *Session) waitSetup(ctx context.Context) error {
	s.mu.Lock()
	pending := s.setup == sessionpb.SetupState_SETUP_STATE_RUNNING
	s.mu.Unlock()
	if !pending {
		return nil
	}
	_, err := safe.StateWatchFor[*typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]]](
		ctx, s.deps.state, lifecycle.ResourceFor(s.id, ReconcileSnapshot{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			input, ok := value.(*typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]])
			if !ok {
				return false, fmt.Errorf("unexpected Session reconcile snapshot resource %T", value)
			}
			return input.TypedSpec().Setup != sessionpb.SetupState_SETUP_STATE_RUNNING, nil
		}),
	)
	return err
}

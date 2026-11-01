package session

import (
	"context"
	"fmt"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

func (m *Manager) publishSession(s *Session, demand bool) {
	m.publishMu.Lock()
	defer m.publishMu.Unlock()
	input, found, err := componentRead[Input](m.ctx, m.state, s.id)
	if err != nil {
		m.fail(err)
		return
	}
	previous := input
	if demand {
		input.Demand++
	}
	s.mu.Lock()
	input.Harness = s.record.Harness
	input.SetupRequested = s.record.SetupScript != ""
	input.Setup, input.Pending, input.ProcessID = s.setup, len(s.pending), s.processID
	input.ProcessRunning = s.process != nil && s.process.Running()
	input.InspectionRunning = s.inspection != nil
	input.Inspections = len(s.reconciliations)
	input.InspectionReady = len(s.reconciliations) > 0 && s.reconciliations[0].ready
	adapter := s.adapter
	s.mu.Unlock()
	input.DispatchBlocked = adapter == nil || adapter.BlocksCommandDispatch()
	if !found || previous != input {
		m.fail(publish(m.ctx, m.state, s.id, input))
	}
	drain := DrainInput{Inspecting: input.Inspections != 0 || input.InspectionRunning}
	previousDrain, found, err := componentRead[DrainInput](m.ctx, m.state, s.id)
	if err != nil {
		m.fail(err)
		return
	}
	if !found || previousDrain != drain {
		m.fail(publish(m.ctx, m.state, s.id, drain))
	}
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
func (s *Session) runtimeState() Runtime {
	value, _, err := componentRead[Runtime](s.deps.ctx, s.deps.state, s.id)
	if err != nil {
		s.deps.fail(err)
	}
	return value
}

func (s *Session) readyLocked() bool {
	current := s.runtimeState()
	return !s.halting.Finished() && (current.Phase == Ready || current.Phase == Stopping) && current.ProcessID == s.processID && s.process != nil && s.process.Running()
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
	_, err := safe.StateWatchFor[*typed.Resource[Input, componentExtension[Input]]](
		ctx, s.deps.state, componentResource(s.id, Input{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			input, ok := value.(*typed.Resource[Input, componentExtension[Input]])
			if !ok {
				return false, fmt.Errorf("unexpected Session input resource %T", value)
			}
			return input.TypedSpec().Setup != runnerpb.SetupState_SETUP_STATE_RUNNING, nil
		}),
	)
	return err
}

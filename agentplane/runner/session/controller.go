package session

import (
	"context"
	"errors"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/codexappserver"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/operation"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

type sessionLifecycleController struct{ manager *Manager }

func (*sessionLifecycleController) Name() string { return ControllerName }
func (c *sessionLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: runnerlifecycle.Namespace, Type: InputType, Kind: controller.InputQPrimary},
			{Namespace: runnerlifecycle.Namespace, Type: runnerlifecycle.DesiredStateType, Kind: controller.InputQMapped},
			{Namespace: runnerlifecycle.Namespace, Type: runnerlifecycle.AggregateStatusType, Kind: controller.InputQMapped},
			{Namespace: runnerlifecycle.Namespace, Type: codexappserver.RuntimeType, Kind: controller.InputQMapped},
			{Namespace: runnerlifecycle.Namespace, Type: OperationType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: RuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(c.manager.workers)),
	}
}

func (c *sessionLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() == OperationType {
		result, found, err := componentRead[operationResult](ctx, r, ptr.ID())
		if err != nil || !found || result.Controller != ControllerName || result.Session == "" {
			return nil, err
		}
		return []resource.Pointer{resource.NewMetadata(runnerlifecycle.Namespace, InputType, result.Session, resource.VersionUndefined)}, nil
	}
	rows, err := safe.ReaderListAll[*typed.Resource[Input, componentExtension[Input]]](ctx, r)
	if err != nil {
		return nil, err
	}
	result := make([]resource.Pointer, 0, rows.Len())
	rows.ForEach(func(row *typed.Resource[Input, componentExtension[Input]]) {
		if ptr.Type() == codexappserver.RuntimeType && row.TypedSpec().Harness != runnerpb.Harness_HARNESS_CODEX {
			return
		}
		result = append(result, row.Metadata())
	})
	return result, nil
}

func (c *sessionLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	s := c.manager.Get(ptr.ID())
	if s == nil {
		return nil
	}
	input, found, err := componentRead[Input](ctx, r, s.id)
	if err != nil || !found {
		return err
	}
	current, _, err := componentRead[Runtime](ctx, r, s.id)
	if err != nil {
		return err
	}
	desiredState, _, err := runnerlifecycle.ReadDesired(ctx, r)
	if err != nil {
		return err
	}
	outer, _, err := runnerlifecycle.ReadStatus(ctx, r)
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := componentWrite(ctx, r, s.id, current)
		if changed {
			s.deps.changed()
		}
		return err
	}
	if desiredState.Mode == runnerlifecycle.Running && current.Generation != desiredState.Generation {
		for _, slot := range []**operation.Operation{&s.halting, &s.sealing} {
			if op, done := s.takeOperation(slot); done {
				c.manager.forgetOperation(op)
			}
		}
		current = Runtime{Generation: desiredState.Generation, SetupAttempt: current.SetupAttempt}
	} else if current.Generation != desiredState.Generation {
		current.Generation = desiredState.Generation
		current.Error = ""
	}
	if op, done := s.takeOperation(&s.setupJob); done {
		c.manager.forgetOperation(op)
		if current.SetupAttempt == "" {
			current.SetupAttempt = op.ID()
		}
		if op.Err() != nil {
			current.Error = op.Err().Error()
			s.deps.fail(op.Err())
			if err := write(); err != nil {
				return err
			}
			return op.Err()
		}
	}
	if input.SetupRequested && current.SetupAttempt == "" {
		s.mu.Lock()
		script, job := s.setupScript, s.setupJob
		s.mu.Unlock()
		if script != nil && job == nil && input.Setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
			op, startErr := s.startOperation(&s.setupJob, func(ctx context.Context) error { return s.runSetup(ctx, *script) })
			if op != nil {
				current.SetupAttempt = op.ID()
			}
			if startErr != nil {
				current.Error = startErr.Error()
				return write()
			}
			if err := write(); err != nil {
				return err
			}
		}
	}
	if op, done := s.takeOperation(&s.preparation); done {
		c.manager.forgetOperation(op)
		// takeOperation refreshes host input synchronously. The completion resource
		// can be queued before the worker's own refresh, so read it again here.
		input, _, err = componentRead[Input](ctx, r, s.id)
		if err != nil {
			return err
		}
		s.mu.Lock()
		processID := s.processID
		live := s.process != nil && s.process.Running()
		s.mu.Unlock()
		if op.Err() == nil && live && desiredState.Mode == runnerlifecycle.Running && current.Phase == Preparing {
			current.Phase, current.ProcessID = Ready, processID
			s.mu.Lock()
			s.processState = runnerpb.ProcessState_PROCESS_STATE_RUNNING
			s.mu.Unlock()
		} else if desiredState.Mode == runnerlifecycle.Running {
			current.Phase = Dormant
			if input.Pending == 0 {
				current.Attempt = input.Demand
			}
		}
	}
	if op, done := s.takeOperation(&s.dispatching); done {
		c.manager.forgetOperation(op)
		if op.Err() != nil {
			return op.Err()
		}
	}
	if op, done := s.takeOperation(&s.inspection); done {
		c.manager.forgetOperation(op)
		if op.Err() != nil {
			return op.Err()
		}
	}
	s.mu.Lock()
	operationStartFailed := s.operationStartFailed
	s.mu.Unlock()
	if operationStartFailed || current.Error != "" && desiredState.Mode == runnerlifecycle.Running {
		return write()
	}
	// Reconciliation runs independently from halt, so native stdout can deliver its replies.
	s.mu.Lock()
	inspectionPending := input.Inspections > 0 && len(s.reconciliations) > 0
	if s.inspection == nil && inspectionPending && input.InspectionReady && s.reconciliations[0].ready {
		pending := s.reconciliations[0]
		s.reconciliations = s.reconciliations[1:]
		s.mu.Unlock()
		if _, startErr := s.startOperation(&s.inspection, func(ctx context.Context) error {
			report, err := pending.adapter.Reconcile(ctx, pending.turnID, false)
			if err == nil {
				err = s.Emit(ctx, observation(report), pending.sources)
			}
			if err != nil {
				s.mu.Lock()
				s.reconciliations = append([]*pendingReconciliation{pending}, s.reconciliations...)
				s.mu.Unlock()
			}
			return err
		}); startErr != nil {
			s.mu.Lock()
			s.reconciliations = append([]*pendingReconciliation{pending}, s.reconciliations...)
			s.mu.Unlock()
			current.Error = startErr.Error()
			return write()
		}
	} else {
		s.mu.Unlock()
	}
	if current.Phase != Halted {
		if err := s.dispatchControl(ctx); err != nil {
			return err
		}
	}
	if desiredState.Mode != runnerlifecycle.Running {
		if outer.Generation != desiredState.Generation || outer.Stage < runnerlifecycle.Halting {
			return write()
		}
		if current.Phase != Halted {
			current.Phase = Stopping
			current.Error = ""
			if err := write(); err != nil {
				return err
			}
			s.mu.Lock()
			halt := s.halting
			s.mu.Unlock()
			if halt == nil {
				if current.Error != "" {
					return write()
				}
				reason := stopClose
				if desiredState.SealUID != "" {
					reason = stopQuiesce
				}
				if _, err := s.startOperation(&s.halting, func(ctx context.Context) error { s.halt(ctx, reason); return nil }); err != nil {
					current.Error = err.Error()
					return write()
				}
				return nil
			}
			s.mu.Lock()
			settled := halt.Finished() && len(s.controlDispatches) == 0
			s.mu.Unlock()
			if settled {
				current.Phase = Halted
				if err := write(); err != nil {
					return err
				}
			}
		}
		if op, done := s.takeOperation(&s.stopping); done {
			c.manager.forgetOperation(op)
			if op.Err() != nil {
				current.Error = op.Err().Error()
				if err := write(); err != nil {
					return err
				}
				return op.Err()
			}
			current.ProcessStopped = true
		}
		if current.Phase == Halted && outer.Stage >= runnerlifecycle.Stopping && !current.ProcessStopped {
			s.mu.Lock()
			shared := s.deps.codexAppServer != nil && s.record.Harness == runnerpb.Harness_HARNESS_CODEX
			s.mu.Unlock()
			if shared {
				status, found, err := codexappserver.ReadRuntime(ctx, r)
				if err != nil {
					return err
				}
				if found && status.Phase == codexappserver.Stopped {
					current.ProcessStopped = true
				}
			} else {
				if _, err := s.startOperation(&s.stopping, s.stopProcess); err != nil {
					current.Error = err.Error()
					return write()
				}
			}
		}
		if op, done := s.takeOperation(&s.sealing); done {
			c.manager.forgetOperation(op)
			if op.Err() != nil {
				return op.Err()
			}
		}
		s.mu.Lock()
		inspected := s.inspection == nil && len(s.reconciliations) == 0
		s.mu.Unlock()
		if current.Phase == Halted && current.ProcessStopped && inspected && outer.Stage >= runnerlifecycle.Sealing && desiredState.SealUID != "" && s.journal.SealState() == nil {
			if _, err := s.startOperation(&s.sealing, func(ctx context.Context) error {
				if err := s.sealJournal(ctx); err != nil {
					return err
				}
				return c.manager.publishSeal(s.id, s.journal.SealState())
			}); err != nil {
				current.Error = err.Error()
				return write()
			}
		}
		return write()
	}
	s.mu.Lock()
	processLive := s.process != nil && s.process.Running()
	processID := s.processID
	pending := len(s.pending)
	s.mu.Unlock()
	if current.Phase == Ready && (!input.ProcessRunning || input.ProcessID != current.ProcessID || !processLive || processID != current.ProcessID) {
		current.Phase = Dormant
		if pending == 0 {
			// Demand from commands completed by this process is already satisfied.
			// Its exit must not turn an idle Session into a fresh startup request.
			current.Attempt = input.Demand
		}
	}
	inspecting := input.InspectionRunning || input.Inspections > 0
	if current.Phase == Dormant && !inspecting && input.Demand > current.Attempt && !s.executionClosed() {
		started, err := s.journal.HasStartedTurn(ctx)
		if err != nil {
			return err
		}
		s.mu.Lock()
		resuming := s.record.NativeSessionID != nil && started
		s.mu.Unlock()
		current.Phase, current.Attempt, current.Resuming = Preparing, input.Demand, resuming
		current.ProcessStopped = false
		current.Error = ""
		if err := write(); err != nil {
			return err
		}
		if _, err := s.startOperation(&s.preparation, func(ctx context.Context) error {
			err := s.ensureReady(ctx)
			if err == nil {
				s.mu.Lock()
				live := s.process != nil && s.process.Running()
				s.mu.Unlock()
				if !live {
					err = errors.New("harness exited before preparation completed")
				}
			}
			if err != nil && !errors.Is(err, context.Canceled) {
				if failure := s.settleUndispatched(s.deps.ctx, "harness startup failed before this command was dispatched: "+err.Error()); failure != nil {
					s.deps.fail(failure)
					return failure
				}
				if failure := s.Emit(s.deps.ctx, observation(&eventpb.HarnessLaunchFailed{Reason: err.Error()}), nil); failure != nil {
					s.deps.fail(failure)
					return failure
				}
			}
			return err
		}); err != nil {
			current.Phase = Dormant
			current.Error = err.Error()
			return write()
		}
		return nil
	}
	if err := write(); err != nil {
		return err
	}
	s.mu.Lock()
	blocked := s.inspection != nil || len(s.reconciliations) > 0 || s.dispatching != nil
	adapter := s.adapter
	s.mu.Unlock()
	if current.Phase != Ready || blocked || input.DispatchBlocked || adapter == nil || adapter.BlocksCommandDispatch() {
		return nil
	}
	commands, err := s.journal.PendingCommands(ctx)
	if err != nil {
		return err
	}
	s.mu.Lock()
	var dispatch []*commandpb.Command
	for _, command := range commands {
		if !s.dispatched[command.CommandId] {
			dispatch = append(dispatch, command)
		}
	}
	s.mu.Unlock()
	if len(dispatch) > 0 {
		if _, err := s.startOperation(&s.dispatching, func(ctx context.Context) error {
			for _, command := range dispatch {
				if err := s.dispatch(ctx, command); err != nil {
					return err
				}
				if adapter.BlocksCommandDispatch() {
					break
				}
			}
			return nil
		}); err != nil {
			current.Error = err.Error()
			return write()
		}
	}
	return nil
}

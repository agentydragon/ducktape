package runner

import (
	"context"
	"errors"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

type sessionLifecycleController struct{ l *lifecycle }

func (*sessionLifecycleController) Name() string { return sessionControllerName }
func (*sessionLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycleNamespace, Type: sessionInputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycleNamespace, Type: runnerGoalType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: bootstrapRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: codexRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: operationResultType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: sessionRuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(16)),
	}
}

func (c *sessionLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() == operationResultType {
		result, found, err := lifecycleRead[operationResultSpec](ctx, r, ptr.ID())
		if err != nil || !found || result.Controller != sessionControllerName || result.Session == "" {
			return nil, err
		}
		return []resource.Pointer{resource.NewMetadata(lifecycleNamespace, sessionInputType, result.Session, resource.VersionUndefined)}, nil
	}
	rows, err := r.List(ctx, resource.NewMetadata(lifecycleNamespace, sessionInputType, "", resource.VersionUndefined))
	if err != nil {
		return nil, err
	}
	result := make([]resource.Pointer, 0, len(rows.Items))
	for _, row := range rows.Items {
		result = append(result, row.Metadata())
	}
	return result, nil
}

func (c *sessionLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	c.l.runner.mu.Lock()
	s := c.l.runner.sessions[ptr.ID()]
	c.l.runner.mu.Unlock()
	if s == nil {
		return nil
	}
	input, found, err := lifecycleRead[sessionInputSpec](ctx, r, s.id)
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycleRead[sessionRuntimeSpec](ctx, r, s.id)
	if err != nil {
		return err
	}
	goal, _, err := lifecycleRead[runnerGoalSpec](ctx, r, "runner")
	if err != nil {
		return err
	}
	outer, _, err := lifecycleRead[runnerRuntimeSpec](ctx, r, "runner")
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, s.id, current)
		if changed {
			s.runner.changed()
		}
		return err
	}
	if goal.Mode == runnerRunning && current.Generation != goal.Generation {
		for _, slot := range []**operation{&s.halting, &s.sealing} {
			if op, done := s.takeOperation(slot); done {
				c.l.forgetOperation(op)
			}
		}
		current = sessionRuntimeSpec{Generation: goal.Generation, SetupAttempt: current.SetupAttempt}
	} else if current.Generation != goal.Generation {
		current.Generation = goal.Generation
		current.Error = ""
	}
	if op, done := s.takeOperation(&s.setupJob); done {
		c.l.forgetOperation(op)
		if current.SetupAttempt == "" {
			current.SetupAttempt = op.id
		}
		if op.err != nil {
			current.Error = op.err.Error()
			s.runner.fail(op.err)
			if err := write(); err != nil {
				return err
			}
			return op.err
		}
	}
	if input.SetupRequested && current.SetupAttempt == "" {
		s.mu.Lock()
		script, job, setup := s.setupScript, s.setupJob, s.setup
		s.mu.Unlock()
		if script != nil && job == nil && setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
			s.startOperation(&s.setupJob, func(ctx context.Context) error { return s.runSetup(ctx, *script) })
			s.mu.Lock()
			current.SetupAttempt = s.setupJob.id
			s.mu.Unlock()
			if err := write(); err != nil {
				return err
			}
		}
	}
	if op, done := s.takeOperation(&s.preparation); done {
		c.l.forgetOperation(op)
		s.mu.Lock()
		processID := s.processID
		live := s.process != nil && s.process.Running()
		s.mu.Unlock()
		if op.err == nil && live && goal.Mode == runnerRunning && current.Phase == executionPreparing {
			current.Phase, current.ProcessID = executionReady, processID
			s.mu.Lock()
			s.processState = runnerpb.ProcessState_PROCESS_STATE_RUNNING
			s.mu.Unlock()
		} else if goal.Mode == runnerRunning {
			current.Phase = executionDormant
			s.mu.Lock()
			pending := len(s.pending)
			s.mu.Unlock()
			if pending == 0 {
				current.Attempt = input.Demand
			}
		}
	}
	if op, done := s.takeOperation(&s.dispatching); done {
		c.l.forgetOperation(op)
		if op.err != nil {
			return op.err
		}
	}
	if op, done := s.takeOperation(&s.inspection); done {
		c.l.forgetOperation(op)
		if op.err != nil {
			return op.err
		}
	}
	// Reconciliation runs independently from halt, so native stdout can deliver its replies.
	s.mu.Lock()
	inspectionPending := len(s.reconciliations) > 0
	if s.inspection == nil && inspectionPending && s.reconciliations[0].ready {
		pending := s.reconciliations[0]
		s.reconciliations = s.reconciliations[1:]
		s.mu.Unlock()
		s.startOperation(&s.inspection, func(ctx context.Context) error {
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
		})
	} else {
		s.mu.Unlock()
	}
	if current.Phase != executionHalted {
		if err := s.dispatchControl(ctx); err != nil {
			return err
		}
	}
	if goal.Mode != runnerRunning {
		if outer.Generation != goal.Generation || outer.Stage < stageHalting {
			return write()
		}
		if current.Phase != executionHalted {
			current.Phase = executionStopping
			if err := write(); err != nil {
				return err
			}
			s.mu.Lock()
			halt := s.halting
			s.mu.Unlock()
			if halt == nil {
				reason := stopClose
				if goal.SealUID != "" {
					reason = stopQuiesce
				}
				s.startOperation(&s.halting, func(ctx context.Context) error { s.halt(ctx, reason); return nil })
				return nil
			}
			s.mu.Lock()
			settled := operationDone(halt) && len(s.controlDispatches) == 0
			s.mu.Unlock()
			if settled {
				current.Phase = executionHalted
				if err := write(); err != nil {
					return err
				}
			}
		}
		if op, done := s.takeOperation(&s.stopping); done {
			c.l.forgetOperation(op)
			if op.err != nil {
				current.Error = op.err.Error()
				if err := write(); err != nil {
					return err
				}
				return op.err
			}
			current.ProcessStopped = true
		}
		if current.Phase == executionHalted && outer.Stage >= stageStopping && !current.ProcessStopped {
			s.mu.Lock()
			shared := s.runner.codex != nil && s.record.Harness == runnerpb.Harness_HARNESS_CODEX
			s.mu.Unlock()
			if shared {
				status, found, err := lifecycleRead[codexRuntimeSpec](ctx, r, "codex")
				if err != nil {
					return err
				}
				if found && status.Phase == codexStopped {
					current.ProcessStopped = true
				}
			} else {
				s.startOperation(&s.stopping, s.stopProcess)
			}
		}
		if op, done := s.takeOperation(&s.sealing); done {
			c.l.forgetOperation(op)
			if op.err != nil {
				return op.err
			}
		}
		s.mu.Lock()
		inspected := s.inspection == nil && len(s.reconciliations) == 0
		s.mu.Unlock()
		if current.Phase == executionHalted && current.ProcessStopped && inspected && outer.Stage >= stageSealing && goal.SealUID != "" && s.journal.SealState() == nil {
			s.startOperation(&s.sealing, func(ctx context.Context) error {
				if err := s.sealJournal(ctx); err != nil {
					return err
				}
				return c.l.publishSeal(s)
			})
		}
		return write()
	}
	s.mu.Lock()
	processLive := s.process != nil && s.process.Running()
	processID := s.processID
	pending := len(s.pending)
	s.mu.Unlock()
	if current.Phase == executionReady && (!processLive || processID != current.ProcessID) {
		current.Phase = executionDormant
		if pending == 0 {
			// Demand from commands completed by this process is already satisfied.
			// Its exit must not turn an idle Session into a fresh startup request.
			current.Attempt = input.Demand
		}
	}
	s.mu.Lock()
	inspecting := s.inspection != nil || len(s.reconciliations) > 0
	s.mu.Unlock()
	if current.Phase == executionDormant && !inspecting && input.Demand > current.Attempt && !s.executionClosed() {
		started, err := s.journal.HasStartedTurn(ctx)
		if err != nil {
			return err
		}
		s.mu.Lock()
		resuming := s.record.NativeSessionID != nil && started
		s.mu.Unlock()
		current.Phase, current.Attempt, current.Resuming = executionPreparing, input.Demand, resuming
		current.ProcessStopped = false
		if err := write(); err != nil {
			return err
		}
		s.startOperation(&s.preparation, func(ctx context.Context) error {
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
				if failure := s.settleUndispatched(s.runner.ctx, "harness startup failed before this command was dispatched: "+err.Error()); failure != nil {
					s.runner.fail(failure)
					return failure
				}
				if failure := s.Emit(s.runner.ctx, observation(&eventpb.HarnessLaunchFailed{Reason: err.Error()}), nil); failure != nil {
					s.runner.fail(failure)
					return failure
				}
			}
			return err
		})
		return nil
	}
	if err := write(); err != nil {
		return err
	}
	s.mu.Lock()
	blocked := s.inspection != nil || len(s.reconciliations) > 0 || s.dispatching != nil
	adapter := s.adapter
	s.mu.Unlock()
	if current.Phase != executionReady || blocked || adapter == nil || adapter.BlocksCommandDispatch() {
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
		s.startOperation(&s.dispatching, func(ctx context.Context) error {
			for _, command := range dispatch {
				if err := s.dispatch(ctx, command); err != nil {
					return err
				}
				if adapter.BlocksCommandDispatch() {
					break
				}
			}
			return nil
		})
	}
	return nil
}

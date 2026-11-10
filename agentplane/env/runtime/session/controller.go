package session

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/oneoff"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/codexappserver"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
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
			{Namespace: lifecycle.Namespace, Type: ReconcileSnapshotType, Kind: controller.InputQPrimary},
			{Namespace: lifecycle.Namespace, Type: OwnerStatusType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: lifecycle.EnvironmentDesiredStateType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: lifecycle.AdmissionCountType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: lifecycle.AggregateStatusType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: codexappserver.StatusType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: retained.IngressAckType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: LifecycleStatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(c.manager.workers)),
	}
}

func (c *sessionLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() == OwnerStatusType || ptr.Type() == retained.IngressAckType {
		return []resource.Pointer{resource.NewMetadata(lifecycle.Namespace, ReconcileSnapshotType, ptr.ID(), resource.VersionUndefined)}, nil
	}
	rows, err := safe.ReaderListAll[*typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]]](ctx, r)
	if err != nil {
		return nil, err
	}
	result := make([]resource.Pointer, 0, rows.Len())
	rows.ForEach(func(row *typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]]) {
		if ptr.Type() == codexappserver.StatusType && row.TypedSpec().Harness != sessionpb.Harness_HARNESS_CODEX {
			return
		}
		result = append(result, row.Metadata())
	})
	return result, nil
}

func (c *sessionLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	owner, found, err := lifecycle.Read[OwnerStatus](ctx, r, ptr.ID())
	if err != nil || !found || !owner.Ready {
		return err
	}
	s := c.manager.Get(ptr.ID())
	if s == nil {
		return nil
	}
	input, found, err := lifecycle.Read[ReconcileSnapshot](ctx, r, s.id)
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycle.Read[LifecycleStatus](ctx, r, s.id)
	if err != nil {
		return err
	}
	desiredState, _, err := lifecycle.ReadEnvironmentDesired(ctx, r)
	if err != nil {
		return err
	}
	outer, _, err := lifecycle.ReadStatus(ctx, r)
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := lifecycle.Write(ctx, r, s.id, current)
		if changed {
			s.deps.changed()
		}
		return err
	}
	if desiredState.Mode == lifecycle.Running && current.EnvironmentGeneration != desiredState.Generation {
		for _, slot := range []**oneoff.Operation{&s.halting, &s.sealing} {
			s.takeOperation(slot)
		}
		current = LifecycleStatus{EnvironmentGeneration: desiredState.Generation, SetupOperationID: current.SetupOperationID}
	} else if current.EnvironmentGeneration != desiredState.Generation {
		// A shutdown retry keeps its settled halt and process stop. Only the
		// seal proof must be reconstructed for the new generation.
		current.EnvironmentGeneration = desiredState.Generation
		current.Error = ""
		current.Sealed = false
		current.SealUID = ""
		current.SealCursor = 0
		current.Delivered = false
		current.AcknowledgementPresent = false
		current.AcknowledgementSealed = false
		current.AcknowledgementCursor = 0
		current.PreFenceSettled = false
	}
	current.AdmissionClosed = s.admissionClosed()
	if op, done := s.takeOperation(&s.setupJob); done {
		if current.SetupOperationID == "" {
			current.SetupOperationID = op.ID()
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
	if input.SetupRequested && current.SetupOperationID == "" {
		s.mu.Lock()
		script, job := string(s.record.SetupScript), s.setupJob
		s.mu.Unlock()
		if script != "" && job == nil && input.Setup == sessionpb.SetupState_SETUP_STATE_RUNNING {
			op, startErr := s.startOperation(&s.setupJob, func(ctx context.Context) error { return s.runSetup(ctx, script) })
			if op != nil {
				current.SetupOperationID = op.ID()
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
		// The operation revision can arrive before the worker refreshes the snapshot.
		// takeOperation refreshes host input synchronously, so read it again here.
		input, _, err = lifecycle.Read[ReconcileSnapshot](ctx, r, s.id)
		if err != nil {
			return err
		}
		s.mu.Lock()
		processID := s.processID
		live := s.process != nil && s.process.Running()
		s.mu.Unlock()
		if op.Err() == nil && live && desiredState.Mode == lifecycle.Running && current.Phase == Preparing {
			current.Phase, current.ProcessID = Ready, processID
			s.mu.Lock()
			s.processState = sessionpb.ProcessState_PROCESS_STATE_RUNNING
			s.mu.Unlock()
		} else if desiredState.Mode == lifecycle.Running {
			current.Phase = Dormant
			if input.Pending == 0 {
				current.ObservedDemand = input.Demand
			}
		}
	}
	if op, done := s.takeOperation(&s.dispatching); done {
		if op.Err() != nil {
			return op.Err()
		}
	}
	if op, done := s.takeOperation(&s.inspection); done {
		if op.Err() != nil {
			return op.Err()
		}
	}
	s.mu.Lock()
	operationStartFailed := s.operationStartFailed
	s.mu.Unlock()
	if desiredState.Mode == lifecycle.Running && (operationStartFailed || current.Error != "") {
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
	if desiredState.Mode != lifecycle.Running {
		if outer.Generation != desiredState.Generation || outer.Stage < lifecycle.Halting {
			return write()
		}
		if current.Error != "" {
			return write()
		}
		if current.Phase != Halted {
			current.Phase = Stopping
			if err := write(); err != nil {
				return err
			}
			s.mu.Lock()
			halt := s.halting
			s.mu.Unlock()
			if halt == nil {
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
			if op.Err() != nil {
				current.Error = op.Err().Error()
				if err := write(); err != nil {
					return err
				}
				return op.Err()
			}
			current.ProcessStopped = true
		}
		if current.Phase == Halted && outer.Stage >= lifecycle.Stopping && !current.ProcessStopped {
			s.mu.Lock()
			shared := s.deps.codexAppServer != nil && s.record.GetPlacement().GetHarness() == sessionpb.Harness_HARNESS_CODEX
			s.mu.Unlock()
			if shared {
				status, found, err := codexappserver.ReadStatus(ctx, r)
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
			if op.Err() != nil {
				current.Error = op.Err().Error()
				if err := write(); err != nil {
					return err
				}
				return op.Err()
			}
		}
		s.mu.Lock()
		sealingPending := s.sealing != nil
		s.mu.Unlock()
		// ProcessStopped follows connection.join (or the shared Codex
		// app-server's stopped status, which follows every End callback). Those
		// callbacks can enqueue inspection after process exit. Once joined,
		// no new inspection can appear during this shutdown.
		current.PreFenceSettled = s.preFenceSettled(current)
		fenceReady := outer.Generation == desiredState.Generation && outer.NativeStopped && outer.Stage >= lifecycle.Sealing
		if current.PreFenceSettled && fenceReady && desiredState.SealUID != "" {
			s.closeAdmission(desiredState.Generation)
			if s.journal.SealState() == nil {
				if _, err := s.startOperation(&s.sealing, func(ctx context.Context) error {
					return s.sealJournal(ctx)
				}); err != nil {
					current.Error = err.Error()
					return write()
				}
				sealingPending = true
			}
		}
		// The seal worker may have settled admission since the initial status
		// read. Publish the cutoff and its seal proof from the same reconcile.
		current.AdmissionClosed = s.admissionClosed()
		current.Sealed = false
		current.SealUID = ""
		current.SealCursor = 0
		current.Delivered = false
		current.AcknowledgementPresent = false
		current.AcknowledgementSealed = false
		current.AcknowledgementCursor = 0
		if seal := s.journal.SealState(); seal != nil && !sealingPending && current.AdmissionClosed && current.PreFenceSettled && fenceReady && desiredState.SealUID != "" && seal.IncarnationUID == desiredState.SealUID {
			current.Sealed = true
			current.SealUID = seal.IncarnationUID
			current.SealCursor = seal.Cursor
			ack, found, err := retained.ReadAcknowledgement(ctx, r, s.id)
			if err != nil {
				return err
			}
			current.AcknowledgementPresent = found
			current.AcknowledgementSealed = ack.Sealed
			current.AcknowledgementCursor = ack.Cursor
			current.Delivered = found && ack.Sealed && ack.Cursor == seal.Cursor
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
			current.ObservedDemand = input.Demand
		}
	}
	inspecting := input.InspectionRunning || input.Inspections > 0
	if current.Phase == Dormant && !inspecting && input.Demand > current.ObservedDemand && !s.executionClosed() {
		started, err := s.journal.HasStartedTurn(ctx)
		if err != nil {
			return err
		}
		s.mu.Lock()
		resuming := s.record.NativeSessionId != nil && started
		s.mu.Unlock()
		if err := s.deps.admission(ctx, desiredState, func(context.Context) error {
			current.Phase, current.ObservedDemand, current.Resuming = Preparing, input.Demand, resuming
			current.ProcessStopped = false
			current.Error = ""
			if err := write(); err != nil {
				return err
			}
			_, err := s.startOperation(&s.preparation, func(ctx context.Context) error {
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
			})
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
		if err := s.deps.admission(ctx, desiredState, func(context.Context) error {
			_, err := s.startOperation(&s.dispatching, func(ctx context.Context) error {
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
			return err
		}); err != nil {
			current.Error = err.Error()
			return write()
		}
	}
	return nil
}

func (s *Session) preFenceSettled(status LifecycleStatus) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return status.Phase == Halted && status.ProcessStopped && status.Error == "" &&
		s.inspection == nil && len(s.reconciliations) == 0
}

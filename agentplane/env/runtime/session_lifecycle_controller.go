package runtime

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/codexappserver"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"go.uber.org/zap"
)

type sessionLifecycleController struct{ runtime *Runtime }

func (*sessionLifecycleController) Name() string { return sessionLifecycleControllerName }
func (*sessionLifecycleController) Inputs() []controller.Input {
	var inputs []controller.Input
	for _, kind := range []string{harnesshostlifecycle.EnvironmentDesiredStateType, harnesshostlifecycle.ExecutionStatusType, executionSignalType, harnesshostlifecycle.AdmissionCountType, session.InspectionStatusType, session.LifecycleStatusType, journalSealType, retained.IngressAckType, codexappserver.StatusType, supervision.DesiredStateType, supervision.StatusType} {
		inputs = append(inputs, controller.Input{Namespace: harnesshostlifecycle.Namespace, Type: kind, Kind: controller.InputWeak})
	}
	return inputs
}

func (*sessionLifecycleController) Outputs() []controller.Output {
	return []controller.Output{{Type: harnesshostlifecycle.AggregateStatusType, Kind: controller.OutputExclusive}}
}

func (c *sessionLifecycleController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}
		if err := c.reconcile(ctx, r); err != nil {
			return err
		}
		r.ResetRestartBackoff()
	}
}

func (c *sessionLifecycleController) reconcile(ctx context.Context, r controller.Runtime) error {
	desiredState, _, err := harnesshostlifecycle.ReadEnvironmentDesired(ctx, r)
	if err != nil {
		return err
	}
	current, _, err := harnesshostlifecycle.ReadStatus(ctx, r)
	if err != nil {
		return err
	}
	if current.Generation != desiredState.Generation {
		current = harnesshostlifecycle.Status{Generation: desiredState.Generation, Stage: harnesshostlifecycle.Draining}
	}
	write := func() error {
		changed, err := harnesshostlifecycle.WriteStatus(ctx, r, current)
		if changed {
			c.runtime.changed()
			r.QueueReconcile()
		}
		return err
	}
	if desiredState.Mode == harnesshostlifecycle.Running {
		current.Stage = harnesshostlifecycle.StageRunning
		current.Blocker = harnesshostlifecycle.Blocker{}
		return write()
	}
	c.runtime.mu.Lock()
	admissions, err := c.runtime.admissionCount(ctx)
	c.runtime.mu.Unlock()
	if err != nil {
		return err
	}
	if admissions != 0 {
		current.Stage = harnesshostlifecycle.Draining
		current.Blocker = harnesshostlifecycle.Blocker{
			Kind: harnesshostlifecycle.AdmissionsPending, ResourceType: harnesshostlifecycle.AdmissionCountType,
			ResourceID: harnesshostlifecycle.ResourceID, ResourcePresent: true, Admissions: uint64(admissions),
		}
		return write()
	}
	ids, err := c.runtime.store.SessionIDs()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := c.runtime.sessions.Load(ctx, id); err != nil {
			return err
		}
	}
	if current.Stage < harnesshostlifecycle.Halting {
		current.Stage = harnesshostlifecycle.Halting
		current.Blocker = harnesshostlifecycle.Blocker{}
		return write()
	}
	sessions := c.runtime.sessions.Inventory()
	for _, s := range sessions {
		status, found, err := session.ReadLifecycleStatus(ctx, r, s.ID())
		if err != nil {
			return err
		}
		// A failure from a prior Host generation is cleared by the Session
		// controller as it observes the shutdown generation. Only surface errors
		// that belong to this teardown; otherwise Close could race that reset.
		if status.EnvironmentGeneration == desiredState.Generation && status.Error != "" {
			current.Error = status.Error
			kind := harnesshostlifecycle.SessionHaltPending
			if status.Phase == session.Halted {
				kind = harnesshostlifecycle.SessionProcessPending
			}
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: kind, ResourceType: session.LifecycleStatusType,
				Error: status.Error, ResourceID: s.ID(), ProcessID: status.ProcessID,
				ResourcePresent: found,
			}
			return write()
		}
		if !found || status.EnvironmentGeneration != desiredState.Generation || status.Phase != session.Halted {
			resourceError := ""
			if status.EnvironmentGeneration == desiredState.Generation {
				resourceError = status.Error
			}
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.SessionHaltPending, ResourceType: session.LifecycleStatusType,
				Error: resourceError, ResourceID: s.ID(), ProcessID: status.ProcessID,
				ResourcePresent: found,
			}
			return write()
		}
	}
	if current.Stage < harnesshostlifecycle.Stopping {
		current.Stage = harnesshostlifecycle.Stopping
		current.Blocker = harnesshostlifecycle.Blocker{}
		if err := write(); err != nil {
			return err
		}
	}
	for _, s := range sessions {
		status, found, err := session.ReadLifecycleStatus(ctx, r, s.ID())
		if err != nil {
			return err
		}
		if !found || status.EnvironmentGeneration != desiredState.Generation || !status.ProcessStopped {
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.SessionProcessPending, ResourceType: session.LifecycleStatusType,
				ResourceID: s.ID(), ProcessID: status.ProcessID, ResourcePresent: found,
			}
			return write()
		}
	}
	if c.runtime.codexAppServer != nil {
		runtime, found, err := codexappserver.ReadStatus(ctx, r)
		if err != nil {
			return err
		}
		if runtime.StopError != "" {
			current.Error = runtime.StopError
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.CodexAppServerPending, ResourceType: codexappserver.StatusType,
				Error: runtime.StopError, ResourceID: codexappserver.ResourceID,
				ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
		if !found || runtime.Phase != codexappserver.Stopped {
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.CodexAppServerPending, ResourceType: codexappserver.StatusType,
				ResourceID: codexappserver.ResourceID, ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
	}
	// Owner-level completion cannot substitute for the native cleanup barrier.
	// Include scripts and workspace helpers, and preserve a failed fence even if
	// a protocol reader already reported the leader's exit.
	processes, err := r.List(ctx, resource.NewMetadata(harnesshostlifecycle.Namespace, supervision.DesiredStateType, "", resource.VersionUndefined))
	if err != nil {
		return err
	}
	for _, process := range processes.Items {
		desiredProcess, found, err := supervision.ReadDesiredState(ctx, r, harnesshostlifecycle.Namespace, process.Metadata().ID())
		if err != nil {
			return err
		}
		if !found {
			continue
		}
		switch desiredProcess.Domain {
		case supervision.RuntimeDomain:
			continue
		case supervision.HarnessDomain:
		default:
			return fmt.Errorf("process %q has unknown domain %q", process.Metadata().ID(), desiredProcess.Domain)
		}
		status, found, err := supervision.ReadStatus(ctx, r, harnesshostlifecycle.Namespace, process.Metadata().ID())
		if err != nil {
			return err
		}
		if status.StopError != "" {
			current.Error = status.StopError
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.ProcessCleanupPending, ResourceType: supervision.StatusType,
				ResourceID: process.Metadata().ID(), ProcessID: process.Metadata().ID(), ResourcePresent: found,
				Error: status.StopError, FinalizersPending: !process.Metadata().Finalizers().Empty(),
			}
			return write()
		}
		if !found || status.Phase != supervision.Exited || !process.Metadata().Finalizers().Empty() {
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.ProcessCleanupPending, ResourceType: supervision.StatusType,
				ResourceID: process.Metadata().ID(), ProcessID: process.Metadata().ID(), ResourcePresent: found,
				FinalizersPending: !process.Metadata().Finalizers().Empty(),
			}
			return write()
		}
	}
	// Process exit can finish an outstanding inspection. Keep its journal and
	// controller alive until the resulting observations have been committed.
	for _, s := range sessions {
		input, found, err := session.ReadInspectionStatus(ctx, r, s.ID())
		if err != nil {
			return err
		}
		if input.Inspecting {
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.SessionInspectionPending, ResourceType: session.InspectionStatusType,
				ResourceID: s.ID(), ResourcePresent: found,
			}
			return write()
		}
	}
	c.runtime.mu.Lock()
	latest, found, err := harnesshostlifecycle.ReadEnvironmentDesired(ctx, c.runtime.lifecycleState)
	if err != nil {
		c.runtime.mu.Unlock()
		return err
	}
	if !found || latest != desiredState {
		c.runtime.mu.Unlock()
		return nil
	}
	current.NativeStopped = true
	fenced := c.runtime.config.CodexExecServer == nil && c.runtime.executionOwner == nil
	if c.runtime.executionLifecycle != nil {
		signal, present, readErr := cosi.Read(ctx, r, executionSignalResource(executionSignal{}))
		if readErr != nil {
			c.runtime.mu.Unlock()
			return readErr
		}
		fenced = present && signal.Fence.LifecycleGeneration == desiredState.Generation && signal.Fence.Error == ""
	}
	c.runtime.mu.Unlock()
	if desiredState.SealUID != "" {
		c.runtime.requestExecutionFence(desiredState.Generation)
	}
	if desiredState.SealUID != "" && !fenced {
		current.Stage = harnesshostlifecycle.Fencing
		return write()
	}
	if desiredState.SealUID == "" && desiredState.Mode != harnesshostlifecycle.Closing {
		current.Blocker = harnesshostlifecycle.Blocker{}
		return write()
	}
	if desiredState.SealUID != "" {
		if current.Stage < harnesshostlifecycle.Sealing {
			current.Stage = harnesshostlifecycle.Sealing
			current.Blocker = harnesshostlifecycle.Blocker{}
			return write()
		}
		for _, s := range sessions {
			seal, found, err := lifecycleRead[journalSealSpec](ctx, r, s.ID())
			if err != nil {
				return err
			}
			if !found || !seal.Present || seal.IncarnationUID != desiredState.SealUID {
				current.Blocker = harnesshostlifecycle.Blocker{
					Kind: harnesshostlifecycle.JournalSealPending, ResourceType: journalSealType,
					ResourceID: s.ID(), ResourcePresent: found, JournalSealPresent: seal.Present,
					JournalSealUID: seal.IncarnationUID, ExpectedJournalSealUID: desiredState.SealUID,
				}
				return write()
			}
		}
	}
	if desiredState.Mode == harnesshostlifecycle.Closing && desiredState.SealUID == "" {
		// Closing the final admission window and publishing completion share
		// the RPC barrier, including late exact-turn controls.
		c.runtime.mu.Lock()
		admissions, err := c.runtime.admissionCount(ctx)
		if err != nil {
			c.runtime.mu.Unlock()
			return err
		}
		if admissions != 0 {
			c.runtime.mu.Unlock()
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.AdmissionsPending, ResourceType: harnesshostlifecycle.AdmissionCountType,
				ResourceID: harnesshostlifecycle.ResourceID, ResourcePresent: true, Admissions: uint64(admissions),
			}
			return write()
		}
		latest := c.runtime.desired
		if latest != desiredState {
			c.runtime.mu.Unlock()
			return nil
		}
		current.Stage = harnesshostlifecycle.Closed
		current.Blocker = harnesshostlifecycle.Blocker{}
		changed, err := harnesshostlifecycle.WriteStatus(ctx, r, current)
		c.runtime.mu.Unlock()
		if changed {
			c.runtime.changed()
			r.QueueReconcile()
		}
		return err
	}
	current.Stage = harnesshostlifecycle.Flushing
	for _, s := range sessions {
		seal, sealFound, err := lifecycleRead[journalSealSpec](ctx, r, s.ID())
		if err != nil {
			return err
		}
		ack, ackFound, err := retained.ReadAcknowledgement(ctx, r, s.ID())
		if err != nil {
			return err
		}
		if !ackFound || !ack.Sealed || ack.Cursor != seal.Cursor {
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.IngressAcknowledgementPending, ResourceType: retained.IngressAckType,
				ResourceID: s.ID(), ResourcePresent: ackFound, JournalSealPresent: sealFound && seal.Present,
				JournalSealUID: seal.IncarnationUID, ExpectedJournalSealUID: desiredState.SealUID,
				JournalSealCursor: seal.Cursor, AcknowledgementPresent: ackFound,
				AcknowledgementSealed: ack.Sealed, AcknowledgementCursor: ack.Cursor,
			}
			return write()
		}
	}
	current.Stage = harnesshostlifecycle.Quiesced
	if desiredState.Mode == harnesshostlifecycle.Closing {
		current.Stage = harnesshostlifecycle.Closed
	}
	current.Blocker = harnesshostlifecycle.Blocker{}
	return write()
}

package runner

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/runner/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"go.uber.org/zap"
)

type runnerLifecycleController struct{ l *lifecycle }

func (*runnerLifecycleController) Name() string { return runnerControllerName }
func (*runnerLifecycleController) Inputs() []controller.Input {
	var inputs []controller.Input
	for _, kind := range []string{runnerlifecycle.DesiredStateType, runnerInputType, session.DrainInputType, session.RuntimeType, journalSealType, ingressAckType, codexexecserver.RuntimeType, codexappserver.RuntimeType, bootstrap.RuntimeType, supervision.DesiredStateType, supervision.StatusType} {
		inputs = append(inputs, controller.Input{Namespace: runnerlifecycle.Namespace, Type: kind, Kind: controller.InputWeak})
	}
	return inputs
}

func (*runnerLifecycleController) Outputs() []controller.Output {
	return []controller.Output{{Type: runnerlifecycle.AggregateStatusType, Kind: controller.OutputExclusive}}
}

func (c *runnerLifecycleController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
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

func (c *runnerLifecycleController) reconcile(ctx context.Context, r controller.Runtime) error {
	desiredState, _, err := runnerlifecycle.ReadDesired(ctx, r)
	if err != nil {
		return err
	}
	current, _, err := runnerlifecycle.ReadStatus(ctx, r)
	if err != nil {
		return err
	}
	if current.Generation != desiredState.Generation {
		current = runnerlifecycle.Status{Generation: desiredState.Generation, Stage: runnerlifecycle.Draining}
	}
	write := func() error {
		changed, err := runnerlifecycle.WriteStatus(ctx, r, current)
		if changed {
			c.l.runner.changed()
			// This controller does not subscribe to RunnerRuntime, so queue the next aggregate step explicitly.
			r.QueueReconcile()
		}
		return err
	}
	if desiredState.Mode == runnerlifecycle.Running {
		current.Stage = runnerlifecycle.StageRunning
		current.Blocker = runnerlifecycle.Blocker{}
		return write()
	}
	c.l.runner.mu.Lock()
	admissions, err := c.l.admissionCount(ctx)
	c.l.runner.mu.Unlock()
	if err != nil {
		return err
	}
	if admissions != 0 {
		current.Stage = runnerlifecycle.Draining
		current.Blocker = runnerlifecycle.Blocker{
			Kind: runnerlifecycle.AdmissionsPending, ResourceType: runnerInputType,
			ResourceID: runnerlifecycle.ResourceID, ResourcePresent: true, Admissions: uint64(admissions),
		}
		return write()
	}
	ids, err := c.l.runner.store.SessionIDs()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := c.l.runner.sessions.Load(ctx, id); err != nil {
			return err
		}
	}
	if current.Stage < runnerlifecycle.Halting {
		current.Stage = runnerlifecycle.Halting
		current.Blocker = runnerlifecycle.Blocker{}
		return write()
	}
	sessions := c.l.runner.sessions.Inventory()
	for _, s := range sessions {
		runtime, found, err := session.ReadRuntime(ctx, r, s.ID())
		if err != nil {
			return err
		}
		// A failure from a prior execution generation is cleared by the Session
		// controller as it observes the shutdown generation. Only surface errors
		// that belong to this teardown; otherwise Close could race that reset.
		if runtime.Generation == desiredState.Generation && runtime.Error != "" {
			current.Error = runtime.Error
			kind := runnerlifecycle.SessionHaltPending
			if runtime.Phase == session.Halted {
				kind = runnerlifecycle.SessionProcessPending
			}
			current.Blocker = runnerlifecycle.Blocker{
				Kind: kind, ResourceType: session.RuntimeType,
				Error: runtime.Error, ResourceID: s.ID(), ProcessID: runtime.ProcessID,
				ResourcePresent: found,
			}
			return write()
		}
		if runtime.Phase != session.Halted {
			resourceError := ""
			if runtime.Generation == desiredState.Generation {
				resourceError = runtime.Error
			}
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.SessionHaltPending, ResourceType: session.RuntimeType,
				Error: resourceError, ResourceID: s.ID(), ProcessID: runtime.ProcessID,
				ResourcePresent: found,
			}
			return write()
		}
	}
	if current.Stage < runnerlifecycle.Stopping {
		current.Stage = runnerlifecycle.Stopping
		current.Blocker = runnerlifecycle.Blocker{}
		if err := write(); err != nil {
			return err
		}
	}
	for _, s := range sessions {
		runtime, found, err := session.ReadRuntime(ctx, r, s.ID())
		if err != nil {
			return err
		}
		if !runtime.ProcessStopped {
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.SessionProcessPending, ResourceType: session.RuntimeType,
				ResourceID: s.ID(), ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
	}
	if c.l.runner.codexAppServer != nil {
		runtime, found, err := codexappserver.ReadRuntime(ctx, r)
		if err != nil {
			return err
		}
		if runtime.StopError != "" {
			current.Error = runtime.StopError
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.CodexAppServerPending, ResourceType: codexappserver.RuntimeType,
				Error: runtime.StopError, ResourceID: codexappserver.ResourceID,
				ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
		if !found || runtime.Phase != codexappserver.Stopped {
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.CodexAppServerPending, ResourceType: codexappserver.RuntimeType,
				ResourceID: codexappserver.ResourceID, ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
	}
	// Relay admission closes with Runner intent. Drain accepted channels before
	// publishing the stage that permits the exec-server component to fence.
	if err := c.l.runner.executorRelay.Drain(ctx); err != nil {
		return fmt.Errorf("drain Codex executor relays: %w", err)
	}
	if current.Stage < runnerlifecycle.Fencing {
		current.Stage = runnerlifecycle.Fencing
		current.Blocker = runnerlifecycle.Blocker{}
		return write()
	}
	if c.l.runner.codexExecServer != nil {
		runtime, found, err := codexexecserver.ReadRuntime(ctx, r)
		if err != nil {
			return err
		}
		if runtime.StopError != "" {
			current.Error = runtime.StopError
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.CodexExecServerPending, ResourceType: codexexecserver.RuntimeType,
				Error: runtime.StopError, ResourceID: codexexecserver.ResourceID,
				ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
		if !found || runtime.Phase != codexexecserver.Stopped {
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.CodexExecServerPending, ResourceType: codexexecserver.RuntimeType,
				ResourceID: codexexecserver.ResourceID, ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
	}
	bootstrapSettled, err := c.l.runner.bootstrapHost.Settled(ctx)
	if err != nil {
		return err
	}
	if !bootstrapSettled {
		runtime, found, err := bootstrap.ReadRuntime(ctx, r)
		if err != nil {
			return err
		}
		current.Blocker = runnerlifecycle.Blocker{
			Kind: runnerlifecycle.BootstrapPending, ResourceType: bootstrap.RuntimeType,
			Error: runtime.Error, ResourceID: bootstrap.ResourceID, ResourcePresent: found,
		}
		return write()
	}
	// Owner-level completion cannot substitute for the native cleanup barrier.
	// Include scripts and workspace helpers, and preserve a failed fence even if
	// a protocol reader already reported the leader's exit.
	processes, err := r.List(ctx, resource.NewMetadata(runnerlifecycle.Namespace, supervision.DesiredStateType, "", resource.VersionUndefined))
	if err != nil {
		return err
	}
	for _, process := range processes.Items {
		status, found, err := supervision.ReadStatus(ctx, r, runnerlifecycle.Namespace, process.Metadata().ID())
		if err != nil {
			return err
		}
		if status.StopError != "" {
			current.Error = status.StopError
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.ProcessCleanupPending, ResourceType: supervision.StatusType,
				ResourceID: process.Metadata().ID(), ProcessID: process.Metadata().ID(), ResourcePresent: found,
				Error: status.StopError, FinalizersPending: !process.Metadata().Finalizers().Empty(),
			}
			return write()
		}
		if !found || status.Phase != supervision.Exited || !process.Metadata().Finalizers().Empty() {
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.ProcessCleanupPending, ResourceType: supervision.StatusType,
				ResourceID: process.Metadata().ID(), ProcessID: process.Metadata().ID(), ResourcePresent: found,
				FinalizersPending: !process.Metadata().Finalizers().Empty(),
			}
			return write()
		}
	}
	// Process exit can finish an outstanding inspection. Keep its journal and
	// controller alive until the resulting observations have been committed.
	for _, s := range sessions {
		input, found, err := session.ReadDrainInput(ctx, r, s.ID())
		if err != nil {
			return err
		}
		if input.Inspecting {
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.SessionInspectionPending, ResourceType: session.DrainInputType,
				ResourceID: s.ID(), ResourcePresent: found,
			}
			return write()
		}
	}
	if desiredState.SealUID != "" {
		if current.Stage < runnerlifecycle.Sealing {
			current.Stage = runnerlifecycle.Sealing
			current.Blocker = runnerlifecycle.Blocker{}
			return write()
		}
		for _, s := range sessions {
			seal, found, err := lifecycleRead[journalSealSpec](ctx, r, s.ID())
			if err != nil {
				return err
			}
			if !found || !seal.Present || seal.IncarnationUID != desiredState.SealUID {
				current.Blocker = runnerlifecycle.Blocker{
					Kind: runnerlifecycle.JournalSealPending, ResourceType: journalSealType,
					ResourceID: s.ID(), ResourcePresent: found, JournalSealPresent: seal.Present,
					JournalSealUID: seal.IncarnationUID, ExpectedJournalSealUID: desiredState.SealUID,
				}
				return write()
			}
		}
	}
	if desiredState.Mode == runnerlifecycle.Closing {
		// Closing the final admission window and publishing completion share
		// the RPC barrier, including late exact-turn controls.
		c.l.runner.mu.Lock()
		admissions, err := c.l.admissionCount(ctx)
		if err != nil {
			c.l.runner.mu.Unlock()
			return err
		}
		if admissions != 0 {
			c.l.runner.mu.Unlock()
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.AdmissionsPending, ResourceType: runnerInputType,
				ResourceID: runnerlifecycle.ResourceID, ResourcePresent: true, Admissions: uint64(admissions),
			}
			return write()
		}
		latest := c.l.desiredState()
		if latest != desiredState {
			c.l.runner.mu.Unlock()
			return nil
		}
		current.Stage = runnerlifecycle.Closed
		current.Blocker = runnerlifecycle.Blocker{}
		changed, err := runnerlifecycle.WriteStatus(ctx, r, current)
		c.l.runner.mu.Unlock()
		if changed {
			c.l.runner.changed()
			r.QueueReconcile()
		}
		return err
	}
	current.Stage = runnerlifecycle.Flushing
	for _, s := range sessions {
		seal, sealFound, err := lifecycleRead[journalSealSpec](ctx, r, s.ID())
		if err != nil {
			return err
		}
		ack, ackFound, err := lifecycleRead[ingressAckSpec](ctx, r, s.ID())
		if err != nil {
			return err
		}
		if !ackFound || !ack.Sealed || ack.Cursor != seal.Cursor {
			current.Blocker = runnerlifecycle.Blocker{
				Kind: runnerlifecycle.IngressAcknowledgementPending, ResourceType: ingressAckType,
				ResourceID: s.ID(), ResourcePresent: ackFound, JournalSealPresent: sealFound && seal.Present,
				JournalSealUID: seal.IncarnationUID, ExpectedJournalSealUID: desiredState.SealUID,
				JournalSealCursor: seal.Cursor, AcknowledgementPresent: ackFound,
				AcknowledgementSealed: ack.Sealed, AcknowledgementCursor: ack.Cursor,
			}
			return write()
		}
	}
	current.Stage = runnerlifecycle.Quiesced
	current.Blocker = runnerlifecycle.Blocker{}
	return write()
}

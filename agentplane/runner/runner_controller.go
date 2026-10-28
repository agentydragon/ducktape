package runner

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/runner/codex/execserver"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"go.uber.org/zap"
)

type runnerLifecycleController struct{ l *lifecycle }

func (*runnerLifecycleController) Name() string { return runnerControllerName }
func (*runnerLifecycleController) Inputs() []controller.Input {
	var inputs []controller.Input
	for _, kind := range []string{runnerlifecycle.DesiredStateType, runnerInputType, sessionInputType, sessionRuntimeType, operationResultType, journalSealType, ingressAckType, execserver.RuntimeType, codexAppServerRuntimeType, bootstrapRuntimeType, supervision.DesiredStateType, supervision.StatusType} {
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
		return write()
	}
	c.l.runner.mu.Lock()
	admissions := c.l.runner.admissions
	c.l.runner.mu.Unlock()
	if admissions != 0 {
		current.Stage = runnerlifecycle.Draining
		return write()
	}
	ids, err := c.l.runner.store.SessionIDs()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := c.l.runner.load(ctx, id); err != nil {
			return err
		}
	}
	if current.Stage < runnerlifecycle.Halting {
		current.Stage = runnerlifecycle.Halting
		return write()
	}
	sessions := c.l.runner.inventory()
	for _, s := range sessions {
		runtime, _, err := lifecycleRead[sessionRuntimeSpec](ctx, r, s.id)
		if err != nil {
			return err
		}
		if runtime.Phase != executionHalted {
			return write()
		}
	}
	if current.Stage < runnerlifecycle.Stopping {
		current.Stage = runnerlifecycle.Stopping
		if err := write(); err != nil {
			return err
		}
	}
	for _, s := range sessions {
		runtime, _, err := lifecycleRead[sessionRuntimeSpec](ctx, r, s.id)
		if err != nil {
			return err
		}
		if runtime.Error != "" {
			current.Error = runtime.Error
			return write()
		}
		if !runtime.ProcessStopped {
			return write()
		}
	}
	if c.l.runner.codexAppServer != nil {
		runtime, found, err := lifecycleRead[codexAppServerRuntimeSpec](ctx, r, codexAppServerResourceID)
		if err != nil {
			return err
		}
		if runtime.StopError != "" {
			current.Error = runtime.StopError
			return write()
		}
		if !found || runtime.Phase != codexAppServerStopped {
			return write()
		}
	}
	if current.Stage < runnerlifecycle.Fencing {
		current.Stage = runnerlifecycle.Fencing
		return write()
	}
	if c.l.runner.codexExecServer != nil {
		runtime, found, err := execserver.ReadRuntime(ctx, r)
		if err != nil {
			return err
		}
		if runtime.StopError != "" {
			current.Error = runtime.StopError
			return write()
		}
		if !found || runtime.Phase != execserver.Stopped {
			return write()
		}
	}
	if !c.l.runner.bootstrapSettled() {
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
			return write()
		}
		if !found || status.Phase != supervision.Exited || !process.Metadata().Finalizers().Empty() {
			return write()
		}
	}
	// Process exit can finish an outstanding inspection. Keep its journal and
	// controller alive until the resulting observations have been committed.
	for _, s := range sessions {
		input, _, err := lifecycleRead[sessionInputSpec](ctx, r, s.id)
		if err != nil {
			return err
		}
		if input.Inspections != 0 || input.InspectionRunning {
			return write()
		}
	}
	if desiredState.SealUID != "" {
		if current.Stage < runnerlifecycle.Sealing {
			current.Stage = runnerlifecycle.Sealing
			return write()
		}
		for _, s := range sessions {
			seal, _, err := lifecycleRead[journalSealSpec](ctx, r, s.id)
			if err != nil {
				return err
			}
			if !seal.Present || seal.IncarnationUID != desiredState.SealUID {
				return write()
			}
		}
	}
	if desiredState.Mode == runnerlifecycle.Closing {
		// Closing the final admission window and publishing completion share
		// the RPC barrier, including late exact-turn controls.
		c.l.runner.mu.Lock()
		if c.l.runner.admissions != 0 {
			c.l.runner.mu.Unlock()
			return nil
		}
		latest := c.l.desiredState()
		if latest != desiredState {
			c.l.runner.mu.Unlock()
			return nil
		}
		current.Stage = runnerlifecycle.Closed
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
		seal, _, err := lifecycleRead[journalSealSpec](ctx, r, s.id)
		if err != nil {
			return err
		}
		ack, found, err := lifecycleRead[ingressAckSpec](ctx, r, s.id)
		if err != nil {
			return err
		}
		if !found || !ack.Sealed || ack.Cursor != seal.Cursor {
			return write()
		}
	}
	current.Stage = runnerlifecycle.Quiesced
	return write()
}

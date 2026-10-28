package runner

import (
	"context"

	"github.com/cosi-project/runtime/pkg/controller"
	"go.uber.org/zap"
)

type runnerLifecycleController struct{ l *lifecycle }

func (*runnerLifecycleController) Name() string { return runnerControllerName }
func (*runnerLifecycleController) Inputs() []controller.Input {
	var inputs []controller.Input
	for _, kind := range []string{runnerDesiredStateType, runnerInputType, sessionInputType, sessionRuntimeType, operationResultType, journalSealType, ingressAckType, codexExecServerRuntimeType, codexAppServerRuntimeType, bootstrapRuntimeType} {
		inputs = append(inputs, controller.Input{Namespace: lifecycleNamespace, Type: kind, Kind: controller.InputWeak})
	}
	return inputs
}

func (*runnerLifecycleController) Outputs() []controller.Output {
	return []controller.Output{{Type: runnerRuntimeType, Kind: controller.OutputExclusive}}
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
	desiredState, _, err := lifecycleRead[runnerDesiredStateSpec](ctx, r, "runner")
	if err != nil {
		return err
	}
	current, _, err := lifecycleRead[runnerRuntimeSpec](ctx, r, "runner")
	if err != nil {
		return err
	}
	if current.Generation != desiredState.Generation {
		current = runnerRuntimeSpec{Generation: desiredState.Generation, Stage: stageDraining}
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, "runner", current)
		if changed {
			c.l.runner.changed()
		}
		return err
	}
	if desiredState.Mode == runnerRunning {
		current.Stage = stageRunning
		return write()
	}
	c.l.runner.mu.Lock()
	admissions := c.l.runner.admissions
	c.l.runner.mu.Unlock()
	if admissions != 0 {
		current.Stage = stageDraining
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
	if current.Stage < stageHalting {
		current.Stage = stageHalting
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
	if current.Stage < stageStopping {
		current.Stage = stageStopping
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
	if current.Stage < stageFencing {
		current.Stage = stageFencing
		return write()
	}
	if c.l.runner.codexExecServer != nil {
		runtime, found, err := lifecycleRead[codexExecServerRuntimeSpec](ctx, r, codexExecServerResourceID)
		if err != nil {
			return err
		}
		if runtime.StopError != "" {
			current.Error = runtime.StopError
			return write()
		}
		if !found || runtime.Phase != codexExecServerStopped {
			return write()
		}
	}
	if !c.l.runner.bootstrapSettled() {
		return write()
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
		if current.Stage < stageSealing {
			current.Stage = stageSealing
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
	if desiredState.Mode == runnerClosing {
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
		current.Stage = stageClosed
		changed, err := lifecycleWrite(ctx, r, "runner", current)
		c.l.runner.mu.Unlock()
		if changed {
			c.l.runner.changed()
		}
		return err
	}
	current.Stage = stageFlushing
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
	current.Stage = stageQuiesced
	return write()
}

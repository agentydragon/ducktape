package harnesshost

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/session"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"go.uber.org/zap"
)

type harnessHostLifecycleController struct{ l *lifecycle }

func harnessHostFenceMatches(applied harnesshostlifecycle.HostIntentStatus, intent *runtimepb.LifecycleIntent, generation uint64) bool {
	return intent != nil && intent.Binding != nil && applied.Applied && applied.Error == "" &&
		applied.Intent.ExecutionFenced && applied.Intent.Generation == generation &&
		applied.Intent == harnesshostlifecycle.HostIntentFromProto(intent)
}

func (*harnessHostLifecycleController) Name() string { return harnessHostControllerName }
func (*harnessHostLifecycleController) Inputs() []controller.Input {
	var inputs []controller.Input
	for _, kind := range []string{harnesshostlifecycle.DesiredStateType, harnesshostlifecycle.HostIntentStatusType, harnessHostInputType, session.DrainInputType, session.LifecycleStatusType, journalSealType, ingressAckType, codexappserver.RuntimeType, supervision.DesiredStateType, supervision.StatusType} {
		inputs = append(inputs, controller.Input{Namespace: harnesshostlifecycle.Namespace, Type: kind, Kind: controller.InputWeak})
	}
	return inputs
}

func (*harnessHostLifecycleController) Outputs() []controller.Output {
	return []controller.Output{{Type: harnesshostlifecycle.AggregateStatusType, Kind: controller.OutputExclusive}}
}

func (c *harnessHostLifecycleController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
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

func (c *harnessHostLifecycleController) reconcile(ctx context.Context, r controller.Runtime) error {
	desiredState, _, err := harnesshostlifecycle.ReadDesired(ctx, r)
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
			c.l.runner.changed()
			// This controller does not subscribe to its output, so queue the next aggregate step explicitly.
			r.QueueReconcile()
		}
		return err
	}
	if desiredState.Mode == harnesshostlifecycle.Running {
		current.Stage = harnesshostlifecycle.StageRunning
		current.Blocker = harnesshostlifecycle.Blocker{}
		return write()
	}
	c.l.runner.mu.Lock()
	admissions, err := c.l.admissionCount(ctx)
	c.l.runner.mu.Unlock()
	if err != nil {
		return err
	}
	if admissions != 0 {
		current.Stage = harnesshostlifecycle.Draining
		current.Blocker = harnesshostlifecycle.Blocker{
			Kind: harnesshostlifecycle.AdmissionsPending, ResourceType: harnessHostInputType,
			ResourceID: harnesshostlifecycle.ResourceID, ResourcePresent: true, Admissions: uint64(admissions),
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
	if current.Stage < harnesshostlifecycle.Halting {
		current.Stage = harnesshostlifecycle.Halting
		current.Blocker = harnesshostlifecycle.Blocker{}
		return write()
	}
	sessions := c.l.runner.sessions.Inventory()
	for _, s := range sessions {
		status, found, err := session.ReadLifecycleStatus(ctx, r, s.ID())
		if err != nil {
			return err
		}
		// A failure from a prior execution generation is cleared by the Session
		// controller as it observes the shutdown generation. Only surface errors
		// that belong to this teardown; otherwise Close could race that reset.
		if status.Generation == desiredState.Generation && status.Error != "" {
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
		if status.Phase != session.Halted {
			resourceError := ""
			if status.Generation == desiredState.Generation {
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
		if !status.ProcessStopped {
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.SessionProcessPending, ResourceType: session.LifecycleStatusType,
				ResourceID: s.ID(), ProcessID: status.ProcessID, ResourcePresent: found,
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
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.CodexAppServerPending, ResourceType: codexappserver.RuntimeType,
				Error: runtime.StopError, ResourceID: codexappserver.ResourceID,
				ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
		if !found || runtime.Phase != codexappserver.Stopped {
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.CodexAppServerPending, ResourceType: codexappserver.RuntimeType,
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
		input, found, err := session.ReadDrainInput(ctx, r, s.ID())
		if err != nil {
			return err
		}
		if input.Inspecting {
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.SessionInspectionPending, ResourceType: session.DrainInputType,
				ResourceID: s.ID(), ResourcePresent: found,
			}
			return write()
		}
	}
	c.l.runner.mu.Lock()
	latest, found, err := harnesshostlifecycle.ReadDesired(ctx, c.l.state)
	if err != nil {
		c.l.runner.mu.Unlock()
		return err
	}
	if !found || latest != desiredState {
		c.l.runner.mu.Unlock()
		return nil
	}
	applied, found, err := cosi.Read(ctx, r, harnesshostlifecycle.HostIntentStatusResource(harnesshostlifecycle.HostIntentStatus{}))
	if err != nil {
		c.l.runner.mu.Unlock()
		return err
	}
	nativeChanged := !c.l.runner.nativeStopped
	c.l.runner.nativeStopped = true
	fenced := found && harnessHostFenceMatches(applied, c.l.runner.intent, desiredState.Generation)
	c.l.runner.mu.Unlock()
	if nativeChanged {
		c.l.runner.changed()
	}
	if desiredState.SealUID != "" && !fenced {
		current.Stage = harnesshostlifecycle.Fencing
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
	if desiredState.Mode == harnesshostlifecycle.Closing {
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
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.AdmissionsPending, ResourceType: harnessHostInputType,
				ResourceID: harnesshostlifecycle.ResourceID, ResourcePresent: true, Admissions: uint64(admissions),
			}
			return write()
		}
		latest := c.l.desiredState()
		if latest != desiredState {
			c.l.runner.mu.Unlock()
			return nil
		}
		current.Stage = harnesshostlifecycle.Closed
		current.Blocker = harnesshostlifecycle.Blocker{}
		changed, err := harnesshostlifecycle.WriteStatus(ctx, r, current)
		c.l.runner.mu.Unlock()
		if changed {
			c.l.runner.changed()
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
		ack, ackFound, err := lifecycleRead[ingressAckSpec](ctx, r, s.ID())
		if err != nil {
			return err
		}
		if !ackFound || !ack.Sealed || ack.Cursor != seal.Cursor {
			current.Blocker = harnesshostlifecycle.Blocker{
				Kind: harnesshostlifecycle.IngressAcknowledgementPending, ResourceType: ingressAckType,
				ResourceID: s.ID(), ResourcePresent: ackFound, JournalSealPresent: sealFound && seal.Present,
				JournalSealUID: seal.IncarnationUID, ExpectedJournalSealUID: desiredState.SealUID,
				JournalSealCursor: seal.Cursor, AcknowledgementPresent: ackFound,
				AcknowledgementSealed: ack.Sealed, AcknowledgementCursor: ack.Cursor,
			}
			return write()
		}
	}
	current.Stage = harnesshostlifecycle.Quiesced
	current.Blocker = harnesshostlifecycle.Blocker{}
	return write()
}

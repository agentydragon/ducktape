package runtime

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/codexappserver"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
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
	for _, kind := range []string{lifecycle.EnvironmentDesiredStateType, executionSignalType, lifecycle.AdmissionCountType, session.OwnerStatusType, session.LifecycleStatusType, codexappserver.StatusType, supervision.DesiredStateType, supervision.StatusType} {
		inputs = append(inputs, controller.Input{Namespace: lifecycle.Namespace, Type: kind, Kind: controller.InputWeak})
	}
	return inputs
}

func (*sessionLifecycleController) Outputs() []controller.Output {
	return []controller.Output{{Type: lifecycle.AggregateStatusType, Kind: controller.OutputExclusive}}
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
	desiredState, _, err := lifecycle.ReadEnvironmentDesired(ctx, r)
	if err != nil {
		return err
	}
	current, _, err := lifecycle.ReadStatus(ctx, r)
	if err != nil {
		return err
	}
	if current.Generation != desiredState.Generation {
		current = lifecycle.Status{Generation: desiredState.Generation, Stage: lifecycle.Draining}
	}
	write := func() error {
		changed, err := lifecycle.WriteStatus(ctx, r, current)
		if changed {
			c.runtime.changed()
			r.QueueReconcile()
		}
		return err
	}
	if desiredState.Mode == lifecycle.Running {
		current.Stage = lifecycle.StageRunning
		current.Blocker = lifecycle.Blocker{}
		return write()
	}
	c.runtime.mu.Lock()
	admissions, err := c.runtime.admissionCount(ctx)
	c.runtime.mu.Unlock()
	if err != nil {
		return err
	}
	if admissions != 0 {
		current.Stage = lifecycle.Draining
		current.Blocker = lifecycle.Blocker{
			Kind: lifecycle.AdmissionsPending, ResourceType: lifecycle.AdmissionCountType,
			ResourceID: lifecycle.ResourceID, ResourcePresent: true, Admissions: uint64(admissions),
		}
		return write()
	}
	ids, err := c.runtime.store.SessionIDs()
	if err != nil {
		return err
	}
	for _, id := range ids {
		owner, found, err := session.ReadOwnerStatus(ctx, r, id)
		if err != nil {
			return err
		}
		if !found || !owner.Ready {
			current.Stage = lifecycle.Draining
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.SessionHaltPending, ResourceType: session.OwnerStatusType,
				ResourceID: id, ResourcePresent: found, Error: owner.Error,
			}
			current.Error = owner.Error
			return write()
		}
	}
	if current.Stage < lifecycle.Halting {
		current.Stage = lifecycle.Halting
		current.Blocker = lifecycle.Blocker{}
		return write()
	}
	// Retained COSI records define the drain membership, including any Session
	// whose live owner or status has not yet become available.
	// Keep each valid observation for the later barriers in this reconciliation.
	// Return on the first blocker before reading a later Session.
	statuses := make([]session.LifecycleStatus, len(ids))
	for i, id := range ids {
		status, found, err := session.ReadLifecycleStatus(ctx, r, id)
		if err != nil {
			return err
		}
		// A failure from a prior Host generation is cleared by the Session
		// controller as it observes the shutdown generation. Only surface errors
		// that belong to this teardown; otherwise Close could race that reset.
		if status.EnvironmentGeneration == desiredState.Generation && status.Error != "" {
			current.Error = status.Error
			kind := lifecycle.SessionHaltPending
			if status.Phase == session.Halted {
				kind = lifecycle.SessionProcessPending
			}
			current.Blocker = lifecycle.Blocker{
				Kind: kind, ResourceType: session.LifecycleStatusType,
				Error: status.Error, ResourceID: id, ProcessID: status.ProcessID,
				ResourcePresent: found,
			}
			return write()
		}
		if !found || status.EnvironmentGeneration != desiredState.Generation || status.Phase != session.Halted {
			resourceError := ""
			if status.EnvironmentGeneration == desiredState.Generation {
				resourceError = status.Error
			}
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.SessionHaltPending, ResourceType: session.LifecycleStatusType,
				Error: resourceError, ResourceID: id, ProcessID: status.ProcessID,
				ResourcePresent: found,
			}
			return write()
		}
		statuses[i] = status
	}
	if current.Stage < lifecycle.Stopping {
		current.Stage = lifecycle.Stopping
		current.Blocker = lifecycle.Blocker{}
		// Publishing Stopping wakes Session controllers. Reconcile again so
		// subsequent barriers observe their response to this stage transition.
		return write()
	}
	for i, id := range ids {
		status := statuses[i]
		if !status.ProcessStopped {
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.SessionProcessPending, ResourceType: session.LifecycleStatusType,
				ResourceID: id, ProcessID: status.ProcessID, ResourcePresent: true,
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
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.CodexAppServerPending, ResourceType: codexappserver.StatusType,
				Error: runtime.StopError, ResourceID: codexappserver.ResourceID,
				ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
		if !found || runtime.Phase != codexappserver.Stopped {
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.CodexAppServerPending, ResourceType: codexappserver.StatusType,
				ResourceID: codexappserver.ResourceID, ProcessID: runtime.ProcessID, ResourcePresent: found,
			}
			return write()
		}
	}
	// Owner-level completion cannot substitute for the native cleanup barrier.
	// Include scripts and workspace helpers, and preserve a failed fence even if
	// a protocol reader already reported the leader's exit.
	processes, err := r.List(ctx, resource.NewMetadata(lifecycle.Namespace, supervision.DesiredStateType, "", resource.VersionUndefined))
	if err != nil {
		return err
	}
	for _, process := range processes.Items {
		desiredProcess, found, err := supervision.ReadDesiredState(ctx, r, lifecycle.Namespace, process.Metadata().ID())
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
		status, found, err := supervision.ReadStatus(ctx, r, lifecycle.Namespace, process.Metadata().ID())
		if err != nil {
			return err
		}
		if status.StopError != "" {
			current.Error = status.StopError
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.ProcessCleanupPending, ResourceType: supervision.StatusType,
				ResourceID: process.Metadata().ID(), ProcessID: process.Metadata().ID(), ResourcePresent: found,
				Error: status.StopError, FinalizersPending: !process.Metadata().Finalizers().Empty(),
			}
			return write()
		}
		if !found || status.Phase != supervision.Exited || !process.Metadata().Finalizers().Empty() {
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.ProcessCleanupPending, ResourceType: supervision.StatusType,
				ResourceID: process.Metadata().ID(), ProcessID: process.Metadata().ID(), ResourcePresent: found,
				FinalizersPending: !process.Metadata().Finalizers().Empty(),
			}
			return write()
		}
	}
	// Process exit can finish an outstanding inspection. Session owns this
	// pre-fence proof, including native exit callbacks and inspection operations.
	// It cannot wait for Sealed: sealing requires the barriers below.
	for i, id := range ids {
		if !statuses[i].PreFenceSettled {
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.SessionInspectionPending, ResourceType: session.LifecycleStatusType,
				ResourceID: id, ResourcePresent: true,
			}
			return write()
		}
	}
	c.runtime.mu.Lock()
	latest, found, err := lifecycle.ReadEnvironmentDesired(ctx, c.runtime.lifecycleState)
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
		current.Stage = lifecycle.Fencing
		return write()
	}
	if desiredState.SealUID == "" && desiredState.Mode != lifecycle.Closing {
		current.Blocker = lifecycle.Blocker{}
		return write()
	}
	if desiredState.SealUID != "" {
		if current.Stage < lifecycle.Sealing {
			current.Stage = lifecycle.Sealing
			current.Blocker = lifecycle.Blocker{}
			return write()
		}
		for i, id := range ids {
			status := statuses[i]
			if !sessionSealComplete(status, true, desiredState) {
				current.Blocker = lifecycle.Blocker{
					Kind: lifecycle.JournalSealPending, ResourceType: session.LifecycleStatusType,
					ResourceID: id, ResourcePresent: true, JournalSealPresent: status.Sealed,
					JournalSealUID: status.SealUID, ExpectedJournalSealUID: desiredState.SealUID,
					JournalSealCursor: status.SealCursor,
				}
				return write()
			}
		}
	}
	if desiredState.Mode == lifecycle.Closing && desiredState.SealUID == "" {
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
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.AdmissionsPending, ResourceType: lifecycle.AdmissionCountType,
				ResourceID: lifecycle.ResourceID, ResourcePresent: true, Admissions: uint64(admissions),
			}
			return write()
		}
		latest := c.runtime.desired
		if latest != desiredState {
			c.runtime.mu.Unlock()
			return nil
		}
		current.Stage = lifecycle.Closed
		current.Blocker = lifecycle.Blocker{}
		changed, err := lifecycle.WriteStatus(ctx, r, current)
		c.runtime.mu.Unlock()
		if changed {
			c.runtime.changed()
			r.QueueReconcile()
		}
		return err
	}
	current.Stage = lifecycle.Flushing
	for i, id := range ids {
		status := statuses[i]
		if !sessionDeliveryComplete(status, true, desiredState) {
			current.Blocker = lifecycle.Blocker{
				Kind: lifecycle.IngressAcknowledgementPending, ResourceType: retained.IngressAckType,
				ResourceID: id, ResourcePresent: status.AcknowledgementPresent,
				JournalSealPresent: status.Sealed,
				JournalSealUID:     status.SealUID, ExpectedJournalSealUID: desiredState.SealUID,
				JournalSealCursor:      status.SealCursor,
				AcknowledgementPresent: status.AcknowledgementPresent,
				AcknowledgementSealed:  status.AcknowledgementSealed,
				AcknowledgementCursor:  status.AcknowledgementCursor,
			}
			return write()
		}
	}
	current.Stage = lifecycle.Quiesced
	if desiredState.Mode == lifecycle.Closing {
		current.Stage = lifecycle.Closed
	}
	current.Blocker = lifecycle.Blocker{}
	return write()
}

// A seal status is complete only once the Session's final admission cutoff
// was enforced and settled for this generation.
func sessionSealComplete(status session.LifecycleStatus, found bool, desired lifecycle.EnvironmentDesired) bool {
	return found && status.EnvironmentGeneration == desired.Generation && status.AdmissionClosed && status.Sealed &&
		status.SealUID == desired.SealUID && desired.SealUID != ""
}

func sessionDeliveryComplete(status session.LifecycleStatus, found bool, desired lifecycle.EnvironmentDesired) bool {
	return sessionSealComplete(status, found, desired) && status.Delivered
}

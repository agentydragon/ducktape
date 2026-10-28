package runner

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const (
	processDesiredStateType = "ProcessDesiredStates.agentplane"
	processStatusType       = "ProcessStatuses.agentplane"
	processInputType        = "ProcessInputs.agentplane"
	processControllerName   = "process-lifecycle"
)

type processDesiredStateSpec struct {
	Owner         string
	Generation    uint64
	StartDeadline time.Time
	StopTimeout   time.Duration
}

type processInputSpec struct {
	Revision uint64
}

type processPhase uint8

const (
	processPending processPhase = iota
	processStarting
	processRunning
	processStopping
	processExited
)

type processStatusSpec struct {
	Owner        string
	Phase        processPhase
	Started      bool
	PID          int
	StartError   string
	StartRefusal string
	ExitError    string
	StopError    string
}

// processLaunchRefusal distinguishes a launch that was rejected by policy or
// capacity from a launch that failed after it was accepted.
type processLaunchRefusal struct {
	reason string
	cause  error
}

func (e *processLaunchRefusal) Error() string {
	if e.cause != nil {
		return e.cause.Error()
	}
	return e.reason
}

func (e *processLaunchRefusal) Unwrap() error { return e.cause }

func (processDesiredStateSpec) resourceType() resource.Type { return processDesiredStateType }
func (value processDesiredStateSpec) DeepCopy() processDesiredStateSpec {
	return value
}

func (processInputSpec) resourceType() resource.Type      { return processInputType }
func (value processInputSpec) DeepCopy() processInputSpec { return value }

func (processStatusSpec) resourceType() resource.Type       { return processStatusType }
func (value processStatusSpec) DeepCopy() processStatusSpec { return value }

type processLifecycleController struct {
	l *lifecycle
}

func (*processLifecycleController) Name() string { return processControllerName }

func (*processLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycleNamespace, Type: processDesiredStateType, Kind: controller.InputQPrimary},
			{Namespace: lifecycleNamespace, Type: processInputType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: processStatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*processLifecycleController) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() != processInputType {
		return nil, nil
	}
	return []resource.Pointer{
		resource.NewMetadata(lifecycleNamespace, processDesiredStateType, ptr.ID(), resource.VersionUndefined),
	}, nil
}

func (c *processLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	if c.l == nil {
		return errors.New("process lifecycle is not configured")
	}
	host := c.l.processes
	id := ptr.ID()
	desiredResource, err := safe.ReaderGetByID[*typed.Resource[processDesiredStateSpec, lifecycleExtension[processDesiredStateSpec]]](ctx, r, id)
	if state.IsNotFoundError(err) {
		return c.cleanupDestroyed(ctx, r, id, host)
	}
	if err != nil {
		return err
	}
	desired := *desiredResource.TypedSpec()
	if desired.Owner == "" {
		return fmt.Errorf("process %q has no owner", id)
	}
	if host == nil {
		return errors.New("process host is not configured")
	}

	current, found, err := lifecycleRead[processStatusSpec](ctx, r, id)
	if err != nil {
		return err
	}
	if !found {
		current = processStatusSpec{Owner: desired.Owner, Phase: processPending}
	} else if current.Owner != desired.Owner {
		return fmt.Errorf("process %q owner changed from %q to %q", id, current.Owner, desired.Owner)
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, id, current)
		if changed {
			c.l.runner.changed()
		}
		return err
	}

	snapshot, registered := host.snapshot(id)
	if desiredResource.Metadata().Phase() == resource.PhaseTearingDown {
		return c.reconcileTeardown(ctx, r, id, desiredResource, &current, snapshot, registered, host, write)
	}

	// Terminal status is retained until the owner destroys its desired state.
	if current.Phase == processExited {
		if !registered || !snapshot.fenced {
			return fmt.Errorf("process %q has terminal status without a fenced host entry", id)
		}
		if desiredResource.Metadata().Finalizers().Has(processControllerName) {
			if err := c.removeFinalizer(ctx, r, id); err != nil {
				return err
			}
		}
		return write()
	}
	if !registered {
		return fmt.Errorf("process %q has desired state but no registered launch callback", id)
	}

	if current.Phase == processPending || current.Phase == processStarting {
		if err := c.ensureFinalizer(ctx, r, id); err != nil {
			return err
		}
		// A teardown can race the initial read. The finalizer makes it safe to
		// re-read here and defer to the stop path if teardown has started.
		desiredResource, err = safe.ReaderGetByID[*typed.Resource[processDesiredStateSpec, lifecycleExtension[processDesiredStateSpec]]](ctx, r, id)
		if state.IsNotFoundError(err) {
			return c.cleanupDestroyed(ctx, r, id, host)
		}
		if err != nil {
			return err
		}
		if desiredResource.Metadata().Phase() == resource.PhaseTearingDown {
			return c.reconcileTeardown(ctx, r, id, desiredResource, &current, snapshot, registered, host, write)
		}
		if !desiredResource.Metadata().Finalizers().Has(processControllerName) {
			return errors.New("process finalizer disappeared before launch")
		}
		if current.Phase == processPending {
			current.Phase = processStarting
			if err := write(); err != nil {
				return err
			}
		}

		// Consume a completed result before claiming or returning from the start
		// slot. Otherwise a start completion event could leave status Starting.
		snapshot, _ = host.snapshot(id)
		current = applyProcessFacts(current, snapshot)
		if snapshot.startDone {
			return c.finishStart(ctx, r, id, &current, snapshot, host, write)
		}
		return host.start(id)
	}

	current = applyProcessFacts(current, snapshot)
	switch current.Phase {
	case processRunning:
		if snapshot.exitObserved {
			current.Phase = processStopping
			if err := write(); err != nil {
				return err
			}
			return host.requestStop(id)
		}
		return write()
	case processStopping:
		if snapshot.fenced {
			current.Phase = processExited
			if err := write(); err != nil {
				return err
			}
			return c.removeFinalizer(ctx, r, id)
		}
		if snapshot.stopDone && snapshot.stopErr != nil {
			current.StopError = snapshot.stopErr.Error()
			return write()
		}
		if !snapshot.stopClaimed {
			if err := host.requestStop(id); err != nil {
				return err
			}
		}
		return write()
	default:
		return fmt.Errorf("process %q has invalid nonterminal phase %d", id, current.Phase)
	}
}

func (c *processLifecycleController) finishStart(ctx context.Context, r controller.QRuntime, id string, current *processStatusSpec, snapshot processHostSnapshot, host *processHost, write func() error) error {
	*current = applyProcessFacts(*current, snapshot)
	if snapshot.startErr != nil {
		if snapshot.hasInstance {
			current.Phase = processStopping
			if err := write(); err != nil {
				return err
			}
			return host.requestStop(id)
		}
		if snapshot.fenced {
			current.Phase = processExited
			if err := write(); err != nil {
				return err
			}
			return c.removeFinalizer(ctx, r, id)
		}
	}
	if snapshot.started {
		if snapshot.exitObserved {
			current.Phase = processStopping
			if err := write(); err != nil {
				return err
			}
			return host.requestStop(id)
		}
		current.Phase = processRunning
	}
	return write()
}

func (c *processLifecycleController) reconcileTeardown(
	ctx context.Context,
	r controller.QRuntime,
	id string,
	desiredResource *typed.Resource[processDesiredStateSpec, lifecycleExtension[processDesiredStateSpec]],
	current *processStatusSpec,
	snapshot processHostSnapshot,
	registered bool,
	host *processHost,
	write func() error,
) error {
	if !registered {
		if desiredResource.Metadata().Finalizers().Has(processControllerName) {
			return fmt.Errorf("process %q has a finalizer but no host registry entry", id)
		}
		if current.Phase != processExited {
			*current = applyProcessFacts(*current, snapshot)
			current.Phase = processExited
			if err := write(); err != nil {
				return err
			}
		}
		return nil
	}

	if err := host.requestStop(id); err != nil {
		return err
	}
	snapshot, _ = host.snapshot(id)
	*current = applyProcessFacts(*current, snapshot)
	if snapshot.stopDone && snapshot.stopErr != nil {
		current.Phase = processStopping
		current.StopError = snapshot.stopErr.Error()
		return write()
	}
	if snapshot.fenced {
		current.Phase = processExited
		if err := write(); err != nil {
			return err
		}
		if desiredResource.Metadata().Finalizers().Has(processControllerName) {
			return c.removeFinalizer(ctx, r, id)
		}
		return nil
	}
	current.Phase = processStopping
	return write()
}

func (c *processLifecycleController) cleanupDestroyed(ctx context.Context, r controller.QRuntime, id string, host *processHost) error {
	if host != nil {
		snapshot, registered := host.snapshot(id)
		if registered && !snapshot.fenced {
			if err := host.requestStop(id); err != nil {
				return err
			}
			return nil
		}
		retired, err := host.retire(ctx, r, id)
		if err != nil {
			return err
		}
		if !retired {
			return nil
		}
	}

	statusPointer := resource.NewMetadata(lifecycleNamespace, processStatusType, id, resource.VersionUndefined)
	if err := r.Destroy(ctx, statusPointer); err != nil && !state.IsNotFoundError(err) {
		return err
	}
	inputPointer := resource.NewMetadata(lifecycleNamespace, processInputType, id, resource.VersionUndefined)
	if err := c.l.state.Destroy(ctx, inputPointer, state.WithDestroyOwner("runner-host")); err != nil && !state.IsNotFoundError(err) {
		return err
	}
	return nil
}

func (c *processLifecycleController) ensureFinalizer(ctx context.Context, r controller.QRuntime, id string) error {
	return r.AddFinalizer(ctx, resource.NewMetadata(lifecycleNamespace, processDesiredStateType, id, resource.VersionUndefined), processControllerName)
}

func (c *processLifecycleController) removeFinalizer(ctx context.Context, r controller.QRuntime, id string) error {
	err := r.RemoveFinalizer(ctx, resource.NewMetadata(lifecycleNamespace, processDesiredStateType, id, resource.VersionUndefined), processControllerName)
	if state.IsNotFoundError(err) {
		return nil
	}
	return err
}

func applyProcessFacts(current processStatusSpec, snapshot processHostSnapshot) processStatusSpec {
	current.Started = current.Started || snapshot.started
	if snapshot.pid != 0 {
		current.PID = snapshot.pid
	}
	if snapshot.startErr != nil {
		current.StartError = snapshot.startErr.Error()
		var refusal *processLaunchRefusal
		if errors.As(snapshot.startErr, &refusal) {
			current.StartRefusal = refusal.reason
		}
	}
	if snapshot.exitObserved && snapshot.exitErr != nil {
		current.ExitError = snapshot.exitErr.Error()
	}
	if snapshot.stopDone && snapshot.stopErr != nil {
		current.StopError = snapshot.stopErr.Error()
	}
	return current
}

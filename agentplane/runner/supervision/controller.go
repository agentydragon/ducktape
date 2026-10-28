package supervision

import (
	"context"
	"errors"
	"fmt"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

type Controller struct {
	host *Host
}

func (*Controller) Name() string { return ControllerName }

func (c *Controller) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: c.host.namespace, Type: DesiredStateType, Kind: controller.InputQPrimary},
			{Namespace: c.host.namespace, Type: inputType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: StatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (c *Controller) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() != inputType {
		return nil, nil
	}
	return []resource.Pointer{
		resource.NewMetadata(c.host.namespace, DesiredStateType, ptr.ID(), resource.VersionUndefined),
	}, nil
}

func (c *Controller) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	if c.host == nil {
		return errors.New("process lifecycle is not configured")
	}
	host := c.host
	id := ptr.ID()
	desiredResource, err := safe.ReaderGet[*typed.Resource[DesiredState, resourceExtension[DesiredState]]](ctx, r, resourceFor(c.host.namespace, id, DesiredState{}).Metadata())
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

	current, found, err := readResource[Status](ctx, r, c.host.namespace, id)
	if err != nil {
		return err
	}
	if !found {
		current = Status{Owner: desired.Owner, Phase: Pending}
	} else if current.Owner != desired.Owner {
		return fmt.Errorf("process %q owner changed from %q to %q", id, current.Owner, desired.Owner)
	}
	write := func() error {
		_, err := writeResource(ctx, r, c.host.namespace, id, current)
		return err
	}

	snapshot, registered := host.snapshot(id)
	if desiredResource.Metadata().Phase() == resource.PhaseTearingDown {
		return c.reconcileTeardown(ctx, r, id, desiredResource, &current, snapshot, registered, host, write)
	}

	// Terminal status is retained until the owner destroys its desired state.
	if current.Phase == Exited {
		if !registered || !snapshot.fenced {
			return fmt.Errorf("process %q has terminal status without a fenced host entry", id)
		}
		if desiredResource.Metadata().Finalizers().Has(ControllerName) {
			if err := c.removeFinalizer(ctx, r, id); err != nil {
				return err
			}
		}
		return write()
	}
	if !registered {
		return fmt.Errorf("process %q has desired state but no registered launch callback", id)
	}

	if current.Phase == Pending || current.Phase == Starting {
		if err := c.ensureFinalizer(ctx, r, id); err != nil {
			return err
		}
		// A teardown can race the initial read. The finalizer makes it safe to
		// re-read here and defer to the stop path if teardown has started.
		desiredResource, err = safe.ReaderGet[*typed.Resource[DesiredState, resourceExtension[DesiredState]]](ctx, r, resourceFor(c.host.namespace, id, DesiredState{}).Metadata())
		if state.IsNotFoundError(err) {
			return c.cleanupDestroyed(ctx, r, id, host)
		}
		if err != nil {
			return err
		}
		if desiredResource.Metadata().Phase() == resource.PhaseTearingDown {
			return c.reconcileTeardown(ctx, r, id, desiredResource, &current, snapshot, registered, host, write)
		}
		if !desiredResource.Metadata().Finalizers().Has(ControllerName) {
			return errors.New("process finalizer disappeared before launch")
		}
		if current.Phase == Pending {
			current.Phase = Starting
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
	case Running:
		if snapshot.exitObserved {
			current.Phase = Stopping
			if err := write(); err != nil {
				return err
			}
			return host.requestStop(id)
		}
		return write()
	case Stopping:
		if snapshot.fenced {
			current.Phase = Exited
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

func (c *Controller) finishStart(ctx context.Context, r controller.QRuntime, id string, current *Status, snapshot processHostSnapshot, host *Host, write func() error) error {
	*current = applyProcessFacts(*current, snapshot)
	if snapshot.startErr != nil {
		if snapshot.hasInstance {
			current.Phase = Stopping
			if err := write(); err != nil {
				return err
			}
			return host.requestStop(id)
		}
		if snapshot.fenced {
			current.Phase = Exited
			if err := write(); err != nil {
				return err
			}
			return c.removeFinalizer(ctx, r, id)
		}
	}
	if snapshot.started {
		if snapshot.exitObserved {
			current.Phase = Stopping
			if err := write(); err != nil {
				return err
			}
			return host.requestStop(id)
		}
		current.Phase = Running
	}
	return write()
}

func (c *Controller) reconcileTeardown(
	ctx context.Context,
	r controller.QRuntime,
	id string,
	desiredResource *typed.Resource[DesiredState, resourceExtension[DesiredState]],
	current *Status,
	snapshot processHostSnapshot,
	registered bool,
	host *Host,
	write func() error,
) error {
	if !registered {
		if desiredResource.Metadata().Finalizers().Has(ControllerName) {
			return fmt.Errorf("process %q has a finalizer but no host registry entry", id)
		}
		if current.Phase != Exited {
			*current = applyProcessFacts(*current, snapshot)
			current.Phase = Exited
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
		current.Phase = Stopping
		current.StopError = snapshot.stopErr.Error()
		return write()
	}
	if snapshot.fenced {
		current.Phase = Exited
		if err := write(); err != nil {
			return err
		}
		if desiredResource.Metadata().Finalizers().Has(ControllerName) {
			return c.removeFinalizer(ctx, r, id)
		}
		return nil
	}
	current.Phase = Stopping
	return write()
}

func (c *Controller) cleanupDestroyed(ctx context.Context, r controller.QRuntime, id string, host *Host) error {
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

	statusPointer := resource.NewMetadata(c.host.namespace, StatusType, id, resource.VersionUndefined)
	if err := r.Destroy(ctx, statusPointer); err != nil && !state.IsNotFoundError(err) {
		return err
	}
	inputPointer := resource.NewMetadata(c.host.namespace, inputType, id, resource.VersionUndefined)
	if err := c.host.state.Destroy(ctx, inputPointer, state.WithDestroyOwner(hostOwner)); err != nil && !state.IsNotFoundError(err) {
		return err
	}
	return nil
}

func (c *Controller) ensureFinalizer(ctx context.Context, r controller.QRuntime, id string) error {
	return r.AddFinalizer(ctx, resource.NewMetadata(c.host.namespace, DesiredStateType, id, resource.VersionUndefined), ControllerName)
}

func (c *Controller) removeFinalizer(ctx context.Context, r controller.QRuntime, id string) error {
	err := r.RemoveFinalizer(ctx, resource.NewMetadata(c.host.namespace, DesiredStateType, id, resource.VersionUndefined), ControllerName)
	if state.IsNotFoundError(err) {
		return nil
	}
	return err
}

func applyProcessFacts(current Status, snapshot processHostSnapshot) Status {
	current.Started = current.Started || snapshot.started
	if snapshot.pid != 0 {
		current.PID = snapshot.pid
	}
	if snapshot.startErr != nil {
		current.StartError = snapshot.startErr.Error()
		var refusal *LaunchRefusal
		if errors.As(snapshot.startErr, &refusal) {
			current.StartRefusal = refusal.Reason
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

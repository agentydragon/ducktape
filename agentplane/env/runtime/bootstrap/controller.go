package bootstrap

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type LifecycleController struct{ host *Host }

func (*LifecycleController) Name() string { return ControllerName }

func (*LifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycle.Namespace, Type: RequestType, Kind: controller.InputQPrimary},
			{Namespace: lifecycle.Namespace, Type: StatusType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: OperationSignalType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: StatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*LifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	input := resource.NewMetadata(lifecycle.Namespace, RequestType, ResourceID, resource.VersionUndefined)
	switch ptr.Type() {
	case RequestType, StatusType:
		return []resource.Pointer{input}, nil
	case OperationSignalType:
		if ptr.ID() != ResourceID {
			return nil, nil
		}
		signal, found, err := lifecycle.Read[operationSignal](ctx, r, ResourceID)
		if err != nil || !found || signal.Revision < operationCompletionRevision {
			return nil, err
		}
		return []resource.Pointer{input}, nil
	default:
		return nil, nil
	}
}

func (c *LifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	h := c.host
	if h == nil {
		return errors.New("bootstrap host is not configured")
	}
	input, found, err := lifecycle.Read[Request](ctx, r, ptr.ID())
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycle.Read[Status](ctx, r, ptr.ID())
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := lifecycle.Write(ctx, r, ptr.ID(), current)
		h.runtimeChanged(changed)
		return err
	}

	if op := h.completedOperation(); op != nil {
		current = outcome(op.Err())
		if err := write(); err != nil {
			return err
		}
		h.consumeOperation(op)
		return nil
	}
	if current.Phase.IsTerminal() || !input.Requested {
		return write()
	}
	if current.Phase == Dormant {
		current = Status{Phase: Running}
		if err := write(); err != nil {
			return err
		}
		if err := h.startOperation(h.runBootstrap); err != nil {
			current = outcome(err)
			return write()
		}
	}
	return nil
}

func outcome(err error) Status {
	if err == nil {
		return Status{Phase: Succeeded}
	}
	if errors.Is(err, ErrInterrupted) {
		return Status{Phase: Interrupted, Error: err.Error()}
	}
	if status.Code(err) == codes.AlreadyExists {
		return Status{Phase: Failed, Error: selectionConflict}
	}
	return Status{Phase: Failed, Error: err.Error()}
}

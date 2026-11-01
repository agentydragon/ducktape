package bootstrap

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
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
			{Namespace: lifecycle.Namespace, Type: InputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycle.Namespace, Type: RuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: OperationType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: RuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*LifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	input := resource.NewMetadata(lifecycle.Namespace, InputType, ResourceID, resource.VersionUndefined)
	switch ptr.Type() {
	case InputType, RuntimeType:
		return []resource.Pointer{input}, nil
	case OperationType:
		result, found, err := componentRead[operationResult](ctx, r, ptr.ID())
		if err != nil || !found || !result.Done {
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
	input, found, err := componentRead[Input](ctx, r, ptr.ID())
	if err != nil || !found {
		return err
	}
	current, _, err := componentRead[Runtime](ctx, r, ptr.ID())
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := componentWrite(ctx, r, ptr.ID(), current)
		h.runtimeChanged(changed)
		return err
	}

	op, done, err := h.takeOperation(ctx)
	if err != nil {
		return err
	}
	if done {
		current = outcome(op.Err())
		return write()
	}
	if isTerminal(current.Phase) || !input.Requested {
		return write()
	}
	if current.Phase == Dormant {
		current = Runtime{Phase: Running}
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

func outcome(err error) Runtime {
	if err == nil {
		return Runtime{Phase: Succeeded}
	}
	if errors.Is(err, ErrInterrupted) {
		return Runtime{Phase: Interrupted, Error: err.Error()}
	}
	if status.Code(err) == codes.AlreadyExists {
		return Runtime{Phase: Failed, Error: selectionConflict}
	}
	return Runtime{Phase: Failed, Error: err.Error()}
}

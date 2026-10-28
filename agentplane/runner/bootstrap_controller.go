package runner

import (
	"context"
	"errors"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	bootstrapInputType      = "BootstrapInputs.agentplane"
	bootstrapRuntimeType    = "BootstrapRuntimes.agentplane"
	bootstrapControllerName = "bootstrap-lifecycle"
)

var errBootstrapInterrupted = errors.New(bootstrapInterruptedError)

type bootstrapInputSpec struct {
	Requested bool
	Demand    uint64
}

type bootstrapPhase uint8

const (
	bootstrapDormant bootstrapPhase = iota
	bootstrapRunning
	bootstrapSucceeded
	bootstrapFailed
	bootstrapInterrupted
)

type bootstrapRuntimeSpec struct {
	Phase bootstrapPhase
	Error string
}

const (
	bootstrapSelectionConflict = "sandbox already selected a different bootstrap script"
	bootstrapInterruptedError  = "the Sandbox bootstrap was interrupted and never runs again; use a new Sandbox"
	bootstrapFailedError       = "the Sandbox bootstrap failed and never runs again; use a new Sandbox"
)

type bootstrapLifecycleController struct{ l *lifecycle }

func (*bootstrapLifecycleController) Name() string { return bootstrapControllerName }

func (*bootstrapLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycleNamespace, Type: bootstrapInputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycleNamespace, Type: runnerGoalType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerInputType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: operationResultType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: bootstrapRuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (c *bootstrapLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	switch ptr.Type() {
	case runnerGoalType, runnerInputType:
		return []resource.Pointer{resource.NewMetadata(lifecycleNamespace, bootstrapInputType, "bootstrap", resource.VersionUndefined)}, nil
	case operationResultType:
		result, found, err := lifecycleRead[operationResultSpec](ctx, r, operationResultType, ptr.ID())
		if err != nil || !found || result.Kind != bootstrapControllerName {
			return nil, err
		}
		return []resource.Pointer{resource.NewMetadata(lifecycleNamespace, bootstrapInputType, "bootstrap", resource.VersionUndefined)}, nil
	default:
		return nil, nil
	}
}

func (c *bootstrapLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	input, found, err := lifecycleRead[bootstrapInputSpec](ctx, r, bootstrapInputType, ptr.ID())
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycleRead[bootstrapRuntimeSpec](ctx, r, bootstrapRuntimeType, ptr.ID())
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, bootstrapRuntimeType, ptr.ID(), current)
		if changed {
			c.l.runner.changed()
		}
		return err
	}

	if op, done := c.l.takeHostOperation(&c.l.runner.bootstrapMu, &c.l.runner.bootstrapOp); done {
		if op.err == nil {
			current = bootstrapRuntimeSpec{Phase: bootstrapSucceeded}
		} else if errors.Is(op.err, errBootstrapInterrupted) {
			current = bootstrapRuntimeSpec{Phase: bootstrapInterrupted, Error: op.err.Error()}
		} else if status.Code(op.err) == codes.AlreadyExists {
			current = bootstrapRuntimeSpec{Phase: bootstrapFailed, Error: bootstrapSelectionConflict}
		} else {
			current = bootstrapRuntimeSpec{Phase: bootstrapFailed, Error: op.err.Error()}
		}
		return write()
	}
	if isTerminalBootstrapPhase(current.Phase) || !input.Requested || input.Demand == 0 {
		return write()
	}
	if current.Phase == bootstrapDormant {
		current = bootstrapRuntimeSpec{Phase: bootstrapRunning}
		if err := write(); err != nil {
			return err
		}
		c.l.startHostOperation(&c.l.runner.bootstrapMu, &c.l.runner.bootstrapOp, bootstrapControllerName, c.l.runner.runBootstrap)
	}
	return nil
}

func isTerminalBootstrapPhase(phase bootstrapPhase) bool {
	return phase == bootstrapSucceeded || phase == bootstrapFailed || phase == bootstrapInterrupted
}

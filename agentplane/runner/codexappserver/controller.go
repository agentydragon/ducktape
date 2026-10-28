package codexappserver

import (
	"context"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/codex/execserver"
	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/operation"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const dependencyBudget = 30 * time.Second

type LifecycleController struct{ host *Host }

func (*LifecycleController) Name() string { return ControllerName }

func (*LifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycle.Namespace, Type: InputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycle.Namespace, Type: lifecycle.DesiredStateType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: lifecycle.AggregateStatusType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: execserver.RuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: OperationType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: RuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*LifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() == OperationType {
		_, found, err := componentRead[operationResult](ctx, r, ptr.ID())
		if err != nil || !found {
			return nil, err
		}
	}
	return []resource.Pointer{resource.NewMetadata(lifecycle.Namespace, InputType, ResourceID, resource.VersionUndefined)}, nil
}

func (c *LifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	// A Desired change must wake blocked attachments even when it leaves Runtime unchanged.
	if c.host != nil {
		c.host.notify()
	}
	input, found, err := ReadInput(ctx, r)
	if err != nil || !found {
		return err
	}
	current, _, err := ReadRuntime(ctx, r)
	if err != nil {
		return err
	}
	desiredState, _, err := lifecycle.ReadDesired(ctx, r)
	if err != nil {
		return err
	}
	outer, _, err := lifecycle.ReadStatus(ctx, r)
	if err != nil {
		return err
	}
	server := c.host
	write := func() error {
		changed, err := componentWrite(ctx, r, ResourceID, current)
		if changed && server != nil {
			server.observe()
		}
		return err
	}
	if server == nil {
		current = Runtime{Phase: Stopped}
		return write()
	}

	// Consume completed protocol initialization and drain operations before making any policy decision.
	// Their slots stay owned until observed here, so these operations cannot overlap.
	if op, done := server.takeOperation(&server.initializing); done {
		if op.Err() != nil {
			current.Phase = Stopped
			current.ProcessID = ""
			current.Error = op.Err().Error()
			current.ExecServerReadyDeadline = time.Time{}
		} else {
			server.mu.Lock()
			p := server.current
			generation := server.initializationGeneration
			server.initializingOwner = nil
			server.initializingOwnerGeneration = 0
			live := p != nil && p.Running()
			server.mu.Unlock()
			if live && desiredState.Mode == lifecycle.Running && generation == desiredState.Generation {
				current.Phase = Ready
				current.ProcessID = p.ID()
				current.Attempt = input.Demand
				current.Error = ""
				current.StopError = ""
				current.ExecServerReadyDeadline = time.Time{}
			} else if live {
				// Initialization admitted under an older RunnerDesiredState may finish physically, but cannot
				// publish readiness. Shutdown will drain it after every Session has halted.
				current.Phase = Initializing
				current.ProcessID = p.ID()
			} else {
				current.Phase = Stopped
				current.ProcessID = ""
				current.Error = "Codex app-server exited before initialization completed"
			}
		}
	}
	if op, done := server.takeOperation(&server.draining); done {
		current.Phase = Stopped
		current.ProcessID = ""
		current.StopError = ""
		current.ExecServerReadyDeadline = time.Time{}
		if op.Err() != nil {
			current.StopError = op.Err().Error()
		}
	}
	var initializing, draining *operation.Operation
	var p *Process
	server.mu.Lock()
	initializing = server.initializing
	draining = server.draining
	server.mu.Unlock()
	if input.ExitGeneration > current.ExitGeneration {
		current.ExitGeneration = input.ExitGeneration
		if current.ProcessID == input.ExitProcessID && initializing == nil && draining == nil {
			current.Phase = Stopped
			current.ProcessID = ""
			current.Error = input.ExitError
			if current.Error == "" {
				current.Error = "Codex app-server exited"
			} else if desiredState.Mode != lifecycle.Running {
				current.StopError = input.ExitError
			}
		}
	}
	if desiredState.Mode == lifecycle.Running && current.Generation != desiredState.Generation {
		server.mu.Lock()
		inactive := server.initializing == nil && server.draining == nil && server.current == nil
		if inactive && server.initializingOwnerGeneration != desiredState.Generation {
			server.initializingOwner = nil
			server.initializingOwnerGeneration = 0
		}
		server.mu.Unlock()
		if inactive {
			// Demand and attempts are scoped to a Runner incarnation. A resumed Runner must wait for
			// fresh demand rather than replaying the previous incarnation's Codex startup request.
			current.Generation = desiredState.Generation
			current.Attempt = 0
			current.ExitGeneration = input.ExitGeneration
			current.Error = ""
			current.StopError = ""
			current.ExecServerReadyDeadline = time.Time{}
			current.Phase = Stopped
			current.ProcessID = ""
		}
	}

	if desiredState.Mode != lifecycle.Running {
		// RunnerLifecycleController reaches lifecycle.Stopping only after every Session has halted. Until
		// then, the app-server remains available to finish those Sessions' native observations.
		if outer.Stage < lifecycle.Stopping {
			return write()
		}
		if current.StopError != "" {
			// A failed COSI process stop leaves ownership uncertain. Preserve that evidence and block
			// repeated drains and Runner's transition into executor fencing.
			return write()
		}
		server.mu.Lock()
		initializing = server.initializing
		draining = server.draining
		p = server.current
		server.mu.Unlock()
		if initializing != nil {
			initializing.Cancel()
			return write()
		}
		if draining != nil {
			current.Phase = Draining
			if p != nil {
				current.ProcessID = p.ID()
			}
			return write()
		}
		if p == nil {
			server.mu.Lock()
			if server.current == nil && server.initializing == nil && server.draining == nil {
				server.initializingOwner = nil
				server.initializingOwnerGeneration = 0
			}
			server.mu.Unlock()
			current.Phase = Stopped
			current.ProcessID = ""
			return write()
		}
		current.Phase = Draining
		current.ProcessID = p.ID()
		if err := write(); err != nil {
			return err
		}
		return server.startOperation(&server.draining, server.drainProcess)
	}

	server.mu.Lock()
	initializing = server.initializing
	draining = server.draining
	p = server.current
	server.mu.Unlock()
	if initializing != nil || draining != nil {
		return write()
	}
	if p != nil {
		if p.Running() {
			if current.Phase == Ready && current.ProcessID == p.ID() {
				if current.Attempt < input.Demand {
					current.Attempt = input.Demand
				}
				return write()
			}
			// A physically live process remains owned by the in-flight initialization/drain sequence.
			return write()
		}
		select {
		case <-p.ended:
			// The reader finalizer clears current after all exit observations are journaled.
			return write()
		default:
			return write()
		}
	}
	if input.Demand <= current.Attempt {
		current.Phase = Stopped
		current.ProcessID = ""
		return write()
	}

	if server.deps.ExecServer != nil {
		executor, found, err := execserver.ReadRuntime(ctx, r)
		if err != nil {
			return err
		}
		if found && executor.Refusal != "" {
			current.Phase = Stopped
			current.Attempt = input.Demand
			current.Error = fmt.Sprintf("Codex exec-server startup refused: %s", executor.Refusal)
			current.ProcessID = ""
			current.ExecServerReadyDeadline = time.Time{}
			return write()
		}
		if found && executor.StopError != "" {
			current.Phase = Stopped
			current.Attempt = input.Demand
			current.Error = fmt.Sprintf("Codex exec-server stopped with error: %s", executor.StopError)
			current.ProcessID = ""
			current.ExecServerReadyDeadline = time.Time{}
			return write()
		}
		now := time.Now()
		if !current.ExecServerReadyDeadline.IsZero() && !now.Before(current.ExecServerReadyDeadline) {
			current.Phase = Stopped
			current.Attempt = input.Demand
			current.Error = fmt.Sprintf("Codex exec-server did not become ready within %s", dependencyBudget)
			current.ExecServerReadyDeadline = time.Time{}
			return write()
		}
		if !found || executor.Phase != execserver.Ready {
			current.Generation = desiredState.Generation
			current.Phase = Initializing
			current.ProcessID = ""
			if current.ExecServerReadyDeadline.IsZero() {
				current.Error = ""
				current.ExecServerReadyDeadline = now.Add(dependencyBudget)
				if err := write(); err != nil {
					return err
				}
				return controller.NewRequeueError(nil, dependencyBudget)
			}
			return controller.NewRequeueError(nil, current.ExecServerReadyDeadline.Sub(now))
		}
		current.ExecServerReadyDeadline = time.Time{}
	}

	current.Phase = Initializing
	current.Generation = desiredState.Generation
	current.Attempt = input.Demand
	current.ExecServerReadyDeadline = time.Time{}
	current.ProcessID = ""
	current.Error = ""
	if err := write(); err != nil {
		return err
	}

	// The Runner's synchronous admission barrier fences initialization against Close and Quiesce.
	return server.deps.Admission(ctx, desiredState, func(ctx context.Context) error {
		server.mu.Lock()
		if server.initializing != nil || server.current != nil {
			server.mu.Unlock()
			return nil
		}
		owner := server.initializingOwner
		if server.initializingOwnerGeneration != desiredState.Generation {
			owner = nil
		}
		server.initializationGeneration = desiredState.Generation
		server.mu.Unlock()
		if err := server.startOperation(&server.initializing, func(ctx context.Context) error {
			return server.initialize(ctx, owner, desiredState.Generation)
		}); err != nil {
			return err
		}
		server.mu.Lock()
		server.initializingOwner = nil
		server.initializingOwnerGeneration = 0
		server.mu.Unlock()
		return nil
	})
}

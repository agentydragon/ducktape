package codexappserver

import (
	"context"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/oneoff"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

type LifecycleController struct{ host *Host }

func (*LifecycleController) Name() string { return ControllerName }

func (*LifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycle.Namespace, Type: LifecycleSignalType, Kind: controller.InputQPrimary},
			{Namespace: lifecycle.Namespace, Type: lifecycle.EnvironmentDesiredStateType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: lifecycle.AdmissionCountType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: lifecycle.AggregateStatusType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: lifecycle.ExecutionStatusType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: StatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*LifecycleController) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, _ controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	return []resource.Pointer{resource.NewMetadata(lifecycle.Namespace, LifecycleSignalType, ResourceID, resource.VersionUndefined)}, nil
}

func (c *LifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	// A Desired change must wake blocked attachments even when it leaves status unchanged.
	if c.host != nil {
		c.host.notify()
	}
	input, found, err := ReadLifecycleSignal(ctx, r)
	if err != nil || !found {
		return err
	}
	current, _, err := ReadStatus(ctx, r)
	if err != nil {
		return err
	}
	desiredState, _, err := lifecycle.ReadEnvironmentDesired(ctx, r)
	if err != nil {
		return err
	}
	outer, _, err := lifecycle.ReadStatus(ctx, r)
	if err != nil {
		return err
	}
	server := c.host
	write := func() error {
		changed, err := lifecycle.Write(ctx, r, ResourceID, current)
		if changed && server != nil {
			server.observe()
		}
		return err
	}
	if server == nil {
		current = Status{Phase: Stopped}
		return write()
	}

	// Consume completed protocol initialization and drain operations before making any policy decision.
	// Their slots stay owned until observed here, so these operations cannot overlap.
	if op, done := server.takeOperation(&server.initializing); done {
		if op.Err() != nil {
			current.Phase = Stopped
			current.ProcessID = ""
			if current.Error != "" {
				if op.Err() != context.Canceled {
					current.Error = fmt.Errorf("%s: %w", current.Error, op.Err()).Error()
				}
			} else {
				current.Error = op.Err().Error()
			}
			current.ObservedDemand = input.Demand
			current.ExecServerReadyDeadline = time.Time{}
		} else {
			server.mu.Lock()
			p := server.current
			environmentGeneration := server.initializationEnvironmentGeneration
			server.initializingOwner = nil
			server.initializingOwnerEnvironmentGeneration = 0
			live := p != nil && p.Running()
			server.mu.Unlock()
			if live && desiredState.Mode == lifecycle.Running && environmentGeneration == desiredState.Generation {
				ready, dependencyErr, err := server.executorDependency(ctx, r, &current, time.Now())
				if err != nil {
					return err
				}
				if current.Error != "" {
					current.Phase = Initializing
					current.ProcessID = p.ID()
					current.ObservedDemand = input.Demand
				} else if dependencyErr != nil {
					current.Phase = Initializing
					current.ProcessID = p.ID()
					current.ObservedDemand = input.Demand
					current.Error = dependencyErr.Error()
				} else if ready {
					current.Phase = Ready
					current.ProcessID = p.ID()
					current.ObservedDemand = input.Demand
					current.Error = ""
					current.StopError = ""
					current.ExecServerReadyDeadline = time.Time{}
				} else {
					current.Phase = Initializing
					current.ProcessID = p.ID()
				}
			} else if live {
				// Initialization admitted under an older EnvironmentRuntimeDesiredState may finish physically, but cannot
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
	var initializing, draining *oneoff.Operation
	var p *Process
	server.mu.Lock()
	initializing = server.initializing
	draining = server.draining
	server.mu.Unlock()
	if input.ExitSequence > current.ExitSequence {
		current.ExitSequence = input.ExitSequence
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
	if desiredState.Mode == lifecycle.Running && current.EnvironmentGeneration != desiredState.Generation {
		server.mu.Lock()
		inactive := server.initializing == nil && server.draining == nil && server.current == nil
		if inactive && server.initializingOwnerEnvironmentGeneration != desiredState.Generation {
			server.initializingOwner = nil
			server.initializingOwnerEnvironmentGeneration = 0
		}
		server.mu.Unlock()
		if inactive {
			// Demand sequences are scoped to a Runtime generation. A resumed Host must wait for
			// fresh demand rather than replaying the previous incarnation's Codex startup request.
			current.EnvironmentGeneration = desiredState.Generation
			current.ObservedDemand = 0
			current.ExitSequence = input.ExitSequence
			current.Error = ""
			current.StopError = ""
			current.ExecServerReadyDeadline = time.Time{}
			current.Phase = Stopped
			current.ProcessID = ""
		}
	}

	if desiredState.Mode != lifecycle.Running {
		// EnvironmentRuntimeLifecycleController reaches lifecycle.Stopping only after every Session has halted. Until
		// then, the app-server remains available to finish those Sessions' native observations.
		if outer.Stage < lifecycle.Stopping {
			return write()
		}
		if current.StopError != "" {
			// A failed COSI process stop leaves ownership uncertain. Preserve that evidence and block
			// repeated drains and the Runtime transition into executor fencing.
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
				server.initializingOwnerEnvironmentGeneration = 0
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
		if err := server.startOperation(&server.draining, server.drainProcess); err != nil {
			current.StopError = err.Error()
			return write()
		}
		return nil
	}

	server.mu.Lock()
	initializing = server.initializing
	draining = server.draining
	p = server.current
	server.mu.Unlock()
	if initializing != nil {
		if !current.ExecServerReadyDeadline.IsZero() {
			ready, dependencyErr, err := server.executorDependency(ctx, r, &current, time.Now())
			if err != nil {
				return err
			}
			if dependencyErr != nil {
				current.ObservedDemand = input.Demand
				current.Error = dependencyErr.Error()
				initializing.Cancel()
			} else if ready {
				current.ExecServerReadyDeadline = time.Time{}
			}
		}
		if p != nil && p.Running() {
			current.ProcessID = p.ID()
		}
		if err := write(); err != nil {
			return err
		}
		if current.Error != "" || current.ExecServerReadyDeadline.IsZero() {
			return nil
		}
		return controller.NewRequeueError(nil, time.Until(current.ExecServerReadyDeadline))
	}
	if draining != nil {
		return write()
	}
	if p != nil {
		if p.Running() {
			if current.Phase == Ready && current.ProcessID == p.ID() {
				if current.ObservedDemand < input.Demand {
					current.ObservedDemand = input.Demand
				}
				return write()
			}
			if current.Phase == Initializing && current.EnvironmentGeneration == desiredState.Generation {
				ready, dependencyErr, err := server.executorDependency(ctx, r, &current, time.Now())
				if err != nil {
					return err
				}
				if current.Error != "" {
					dependencyErr = fmt.Errorf("%s", current.Error)
				} else if dependencyErr == nil && ready {
					current.Phase = Ready
					current.ProcessID = p.ID()
					current.ObservedDemand = input.Demand
					current.Error = ""
					current.StopError = ""
					current.ExecServerReadyDeadline = time.Time{}
					return write()
				}
				if dependencyErr != nil {
					current.Phase = Draining
					current.ProcessID = p.ID()
					current.ObservedDemand = input.Demand
					current.Error = dependencyErr.Error()
					current.ExecServerReadyDeadline = time.Time{}
					if err := server.startOperation(&server.draining, server.drainProcess); err != nil {
						current.Phase = Stopped
						current.StopError = err.Error()
					}
					return write()
				}
				current.ProcessID = p.ID()
				if err := write(); err != nil {
					return err
				}
				if !current.ExecServerReadyDeadline.IsZero() {
					return controller.NewRequeueError(nil, time.Until(current.ExecServerReadyDeadline))
				}
				return nil
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
	if input.Demand <= current.ObservedDemand {
		current.Phase = Stopped
		current.ProcessID = ""
		return write()
	}

	ready, dependencyErr, err := server.executorDependency(ctx, r, &current, time.Now())
	if err != nil {
		return err
	}
	if dependencyErr != nil {
		current.Phase = Stopped
		current.ObservedDemand = input.Demand
		current.Error = dependencyErr.Error()
		current.ProcessID = ""
		current.ExecServerReadyDeadline = time.Time{}
		return write()
	}
	if ready {
		current.ExecServerReadyDeadline = time.Time{}
	}

	// The Runtime admission barrier fences initialization against Close and Quiesce.
	if err := server.deps.Admission(ctx, desiredState, func(ctx context.Context) error {
		current.Phase = Initializing
		current.EnvironmentGeneration = desiredState.Generation
		current.ObservedDemand = input.Demand
		current.ProcessID = ""
		current.Error = ""
		if err := write(); err != nil {
			return err
		}

		server.mu.Lock()
		if server.initializing != nil || server.current != nil {
			server.mu.Unlock()
			return nil
		}
		owner := server.initializingOwner
		if server.initializingOwnerEnvironmentGeneration != desiredState.Generation {
			owner = nil
		}
		server.initializationEnvironmentGeneration = desiredState.Generation
		server.mu.Unlock()
		if err := server.startOperation(&server.initializing, func(ctx context.Context) error {
			return server.initialize(ctx, owner, desiredState.Generation)
		}); err != nil {
			server.mu.Lock()
			server.initializingOwner = nil
			server.initializingOwnerEnvironmentGeneration = 0
			server.mu.Unlock()
			current.Phase = Stopped
			current.ObservedDemand = input.Demand
			current.ProcessID = ""
			current.Error = err.Error()
			current.ExecServerReadyDeadline = time.Time{}
			return write()
		}
		server.mu.Lock()
		server.initializingOwner = nil
		server.initializingOwnerEnvironmentGeneration = 0
		server.mu.Unlock()
		return nil
	}); err != nil {
		return err
	}
	if !current.ExecServerReadyDeadline.IsZero() {
		return controller.NewRequeueError(nil, time.Until(current.ExecServerReadyDeadline))
	}
	return nil
}

func (server *Host) executorDependency(
	ctx context.Context,
	r controller.QRuntime,
	current *Status,
	now time.Time,
) (bool, error, error) {
	if server.deps.ExecServerURL == "" {
		return true, nil, nil
	}
	if server.deps.ExecutionReady == nil {
		return false, fmt.Errorf("execution readiness is required"), nil
	}
	ready, err := server.deps.ExecutionReady(ctx)
	if err != nil {
		return false, err, nil
	}
	if ready {
		return true, nil, nil
	}
	if current.ExecServerReadyDeadline.IsZero() {
		current.ExecServerReadyDeadline = now.Add(server.deps.Readiness.ExecServerDependencyTimeout)
	}
	if !now.Before(current.ExecServerReadyDeadline) {
		return false, fmt.Errorf("Codex exec-server did not become ready within %s", server.deps.Readiness.ExecServerDependencyTimeout), nil
	}
	return false, nil, nil
}

package runner

import (
	"context"
	"fmt"
	"time"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const (
	codexAppServerInputType      = "CodexAppServerInputs.agentplane"
	codexAppServerRuntimeType    = "CodexAppServerRuntimes.agentplane"
	codexAppServerControllerName = "codex-app-server-lifecycle"
	codexAppServerResourceID     = "codex-app-server"
)

type codexAppServerInputSpec struct {
	Demand         uint64
	ExitGeneration uint64
	ExitProcessID  string
	ExitError      string
}

type codexAppServerPhase uint8

const (
	codexAppServerStopped codexAppServerPhase = iota
	codexAppServerInitializing
	codexAppServerReady
	codexAppServerDraining
)

type codexAppServerRuntimeSpec struct {
	Phase                   codexAppServerPhase
	Generation              uint64
	Attempt                 uint64
	ExitGeneration          uint64
	ExecServerReadyDeadline time.Time
	ProcessID               string
	Error                   string
	StopError               string
}

const codexAppServerDependencyBudget = 30 * time.Second

type codexAppServerLifecycleController struct {
	l               *lifecycle
	host            *codexAppServer
	codexExecServer *codexExecServer
}

func (*codexAppServerLifecycleController) Name() string { return codexAppServerControllerName }

func (*codexAppServerLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycleNamespace, Type: codexAppServerInputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycleNamespace, Type: runnerDesiredStateType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerInputType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: codexExecServerRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: operationResultType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: codexAppServerRuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*codexAppServerLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() == operationResultType {
		result, found, err := lifecycleRead[operationResultSpec](ctx, r, ptr.ID())
		if err != nil || !found || result.Session != "" || result.Controller != codexAppServerControllerName {
			return nil, err
		}
	}
	return []resource.Pointer{resource.NewMetadata(lifecycleNamespace, codexAppServerInputType, codexAppServerResourceID, resource.VersionUndefined)}, nil
}

func (c *codexAppServerLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	input, found, err := lifecycleRead[codexAppServerInputSpec](ctx, r, codexAppServerResourceID)
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycleRead[codexAppServerRuntimeSpec](ctx, r, codexAppServerResourceID)
	if err != nil {
		return err
	}
	desiredState, _, err := lifecycleRead[runnerDesiredStateSpec](ctx, r, "runner")
	if err != nil {
		return err
	}
	outer, _, err := lifecycleRead[runnerRuntimeSpec](ctx, r, "runner")
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, codexAppServerResourceID, current)
		if changed {
			c.l.runner.changed()
		}
		return err
	}
	server := c.host
	if server == nil {
		current = codexAppServerRuntimeSpec{Phase: codexAppServerStopped}
		return write()
	}

	// Consume completed protocol initialization and drain operations before making any policy decision.
	// Their slots stay owned until observed here, so these operations cannot overlap.
	if op, done := c.l.takeHostOperation(&server.mu, &server.initializing); done {
		if op.err != nil {
			current.Phase = codexAppServerStopped
			current.ProcessID = ""
			current.Error = op.err.Error()
			current.ExecServerReadyDeadline = time.Time{}
		} else {
			server.mu.Lock()
			p := server.current
			generation := server.initializationGeneration
			server.initializingOwner = nil
			server.initializingOwnerGeneration = 0
			live := p != nil && p.process.Running()
			server.mu.Unlock()
			if live && desiredState.Mode == runnerRunning && generation == desiredState.Generation {
				current.Phase = codexAppServerReady
				current.ProcessID = p.process.ID()
				current.Attempt = input.Demand
				current.Error = ""
				current.StopError = ""
				current.ExecServerReadyDeadline = time.Time{}
			} else if live {
				// Initialization admitted under an older RunnerDesiredState may finish physically, but cannot
				// publish readiness. Shutdown will drain it after every Session has halted.
				current.Phase = codexAppServerInitializing
				current.ProcessID = p.process.ID()
			} else {
				current.Phase = codexAppServerStopped
				current.ProcessID = ""
				current.Error = "Codex app-server exited before initialization completed"
			}
		}
	}
	if op, done := c.l.takeHostOperation(&server.mu, &server.draining); done {
		current.Phase = codexAppServerStopped
		current.ProcessID = ""
		current.StopError = ""
		current.ExecServerReadyDeadline = time.Time{}
		if op.err != nil {
			current.StopError = op.err.Error()
		}
	}
	var initializing, draining *operation
	var p *codexAppServerProcess
	server.mu.Lock()
	initializing = server.initializing
	draining = server.draining
	server.mu.Unlock()
	if input.ExitGeneration > current.ExitGeneration {
		current.ExitGeneration = input.ExitGeneration
		if current.ProcessID == input.ExitProcessID && initializing == nil && draining == nil {
			current.Phase = codexAppServerStopped
			current.ProcessID = ""
			current.Error = input.ExitError
			if current.Error == "" {
				current.Error = "Codex app-server exited"
			} else if desiredState.Mode != runnerRunning {
				current.StopError = input.ExitError
			}
		}
	}
	if desiredState.Mode == runnerRunning && current.Generation != desiredState.Generation {
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
			current.Phase = codexAppServerStopped
			current.ProcessID = ""
		}
	}

	if desiredState.Mode != runnerRunning {
		// RunnerLifecycleController reaches stageStopping only after every Session has halted. Until
		// then, the app-server remains available to finish those Sessions' native observations.
		if outer.Stage < stageStopping {
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
			if initializing.cancel != nil {
				initializing.cancel()
			}
			return write()
		}
		if draining != nil {
			current.Phase = codexAppServerDraining
			if p != nil {
				current.ProcessID = p.process.ID()
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
			current.Phase = codexAppServerStopped
			current.ProcessID = ""
			return write()
		}
		current.Phase = codexAppServerDraining
		current.ProcessID = p.process.ID()
		if err := write(); err != nil {
			return err
		}
		c.l.startHostOperation(&server.mu, &server.draining, codexAppServerControllerName, server.drainProcess)
		return nil
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
		if p.process.Running() {
			if current.Phase == codexAppServerReady && current.ProcessID == p.process.ID() {
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
		current.Phase = codexAppServerStopped
		current.ProcessID = ""
		return write()
	}

	if c.codexExecServer != nil {
		executor, found, err := lifecycleRead[codexExecServerRuntimeSpec](ctx, r, codexExecServerResourceID)
		if err != nil {
			return err
		}
		if found && executor.Refusal != "" {
			current.Phase = codexAppServerStopped
			current.Attempt = input.Demand
			current.Error = fmt.Sprintf("Codex exec-server startup refused: %s", executor.Refusal)
			current.ProcessID = ""
			current.ExecServerReadyDeadline = time.Time{}
			return write()
		}
		if found && executor.StopError != "" {
			current.Phase = codexAppServerStopped
			current.Attempt = input.Demand
			current.Error = fmt.Sprintf("Codex exec-server stopped with error: %s", executor.StopError)
			current.ProcessID = ""
			current.ExecServerReadyDeadline = time.Time{}
			return write()
		}
		now := time.Now()
		if !current.ExecServerReadyDeadline.IsZero() && !now.Before(current.ExecServerReadyDeadline) {
			current.Phase = codexAppServerStopped
			current.Attempt = input.Demand
			current.Error = fmt.Sprintf("Codex exec-server did not become ready within %s", codexAppServerDependencyBudget)
			current.ExecServerReadyDeadline = time.Time{}
			return write()
		}
		if !found || executor.Phase != codexExecServerReady {
			current.Generation = desiredState.Generation
			current.Phase = codexAppServerInitializing
			current.ProcessID = ""
			if current.ExecServerReadyDeadline.IsZero() {
				current.Error = ""
				current.ExecServerReadyDeadline = now.Add(codexAppServerDependencyBudget)
				if err := write(); err != nil {
					return err
				}
				return controller.NewRequeueError(nil, codexAppServerDependencyBudget)
			}
			return controller.NewRequeueError(nil, current.ExecServerReadyDeadline.Sub(now))
		}
		current.ExecServerReadyDeadline = time.Time{}
	}

	current.Phase = codexAppServerInitializing
	current.Generation = desiredState.Generation
	current.Attempt = input.Demand
	current.ExecServerReadyDeadline = time.Time{}
	current.ProcessID = ""
	current.Error = ""
	if err := write(); err != nil {
		return err
	}

	// Runner.mu is the synchronous admission barrier shared with Close and Quiesce. The managed-process
	// API honors cancellation when publishing process intent; this slot serializes the Codex initialization handshake.
	c.l.runner.mu.Lock()
	latest := c.l.desiredState()
	if latest != desiredState || latest.Mode != runnerRunning {
		c.l.runner.mu.Unlock()
		return nil
	}
	server.mu.Lock()
	if server.initializing != nil || server.current != nil {
		server.mu.Unlock()
		c.l.runner.mu.Unlock()
		return nil
	}
	owner := server.initializingOwner
	if server.initializingOwnerGeneration != latest.Generation {
		owner = nil
	}
	server.initializingOwner = nil
	server.initializingOwnerGeneration = 0
	server.initializationGeneration = latest.Generation
	server.mu.Unlock()
	c.l.startHostOperation(&server.mu, &server.initializing, codexAppServerControllerName, func(ctx context.Context) error {
		return server.initialize(ctx, owner)
	})
	c.l.runner.mu.Unlock()
	return nil
}

func (codexAppServerInputSpec) resourceType() resource.Type             { return codexAppServerInputType }
func (value codexAppServerInputSpec) DeepCopy() codexAppServerInputSpec { return value }

func (codexAppServerRuntimeSpec) resourceType() resource.Type               { return codexAppServerRuntimeType }
func (value codexAppServerRuntimeSpec) DeepCopy() codexAppServerRuntimeSpec { return value }

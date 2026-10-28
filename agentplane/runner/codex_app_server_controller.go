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
	codexAppServerStarting
	codexAppServerReady
	codexAppServerStopping
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

	// Consume completed native effects before making any policy decision. The slot remains owned until
	// the result is observed here, so a second start or stop cannot overlap an uncertain operation.
	if op, done := c.l.takeHostOperation(&server.mu, &server.starting); done {
		if op.err != nil {
			current.Phase = codexAppServerStopped
			current.ProcessID = ""
			current.Error = op.err.Error()
			current.ExecServerReadyDeadline = time.Time{}
		} else {
			server.mu.Lock()
			p := server.current
			generation := server.startGeneration
			server.startOwner = nil
			server.startOwnerGeneration = 0
			live := p != nil && p.process.Running()
			server.mu.Unlock()
			if live && desiredState.Mode == runnerRunning && generation == desiredState.Generation {
				current.Phase = codexAppServerReady
				current.ProcessID = p.id
				current.Attempt = input.Demand
				current.Error = ""
				current.StopError = ""
				current.ExecServerReadyDeadline = time.Time{}
			} else if live {
				// A start admitted under an older RunnerDesiredState may finish physically, but cannot publish
				// readiness. Shutdown will stop it after every Session has halted.
				current.Phase = codexAppServerStarting
				current.ProcessID = p.id
			} else {
				current.Phase = codexAppServerStopped
				current.ProcessID = ""
				current.Error = "Codex app-server exited before initialization completed"
			}
		}
	}
	if op, done := c.l.takeHostOperation(&server.mu, &server.stopping); done {
		current.Phase = codexAppServerStopped
		current.ProcessID = ""
		current.StopError = ""
		current.ExecServerReadyDeadline = time.Time{}
		if op.err != nil {
			current.StopError = op.err.Error()
		}
	}
	var starting, stopping *operation
	var p *codexAppServerProcess
	server.mu.Lock()
	starting = server.starting
	stopping = server.stopping
	server.mu.Unlock()
	if input.ExitGeneration > current.ExitGeneration {
		current.ExitGeneration = input.ExitGeneration
		if current.ProcessID == input.ExitProcessID && starting == nil && stopping == nil {
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
		inactive := server.starting == nil && server.stopping == nil && server.current == nil
		if inactive && server.startOwnerGeneration != desiredState.Generation {
			server.startOwner = nil
			server.startOwnerGeneration = 0
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
			// A failed physical stop leaves ownership uncertain. Preserve that evidence and block
			// both duplicate stop attempts and Runner's transition into executor fencing.
			return write()
		}
		server.mu.Lock()
		starting = server.starting
		stopping = server.stopping
		p = server.current
		server.mu.Unlock()
		if starting != nil {
			if starting.cancel != nil {
				starting.cancel()
			}
			return write()
		}
		if stopping != nil {
			current.Phase = codexAppServerStopping
			if p != nil {
				current.ProcessID = p.id
			}
			return write()
		}
		if p == nil {
			server.mu.Lock()
			if server.current == nil && server.starting == nil && server.stopping == nil {
				server.startOwner = nil
				server.startOwnerGeneration = 0
			}
			server.mu.Unlock()
			current.Phase = codexAppServerStopped
			current.ProcessID = ""
			return write()
		}
		current.Phase = codexAppServerStopping
		current.ProcessID = p.id
		if err := write(); err != nil {
			return err
		}
		c.l.startHostOperation(&server.mu, &server.stopping, codexAppServerControllerName, server.stopProcess)
		return nil
	}

	server.mu.Lock()
	starting = server.starting
	stopping = server.stopping
	p = server.current
	server.mu.Unlock()
	if starting != nil || stopping != nil {
		return write()
	}
	if p != nil {
		if p.process.Running() {
			if current.Phase == codexAppServerReady && current.ProcessID == p.id {
				if current.Attempt < input.Demand {
					current.Attempt = input.Demand
				}
				return write()
			}
			// A physically live process remains owned by the in-flight start/stop state machine.
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
			current.Phase = codexAppServerStarting
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

	current.Phase = codexAppServerStarting
	current.Generation = desiredState.Generation
	current.Attempt = input.Demand
	current.ExecServerReadyDeadline = time.Time{}
	current.ProcessID = ""
	current.Error = ""
	if err := write(); err != nil {
		return err
	}

	// Runner.mu is the synchronous admission barrier shared with Close and Quiesce. Once the slot is
	// claimed under that barrier, a concurrent desired state change waits until the native effect is admitted.
	c.l.runner.mu.Lock()
	latest := c.l.desiredState()
	if latest != desiredState || latest.Mode != runnerRunning {
		c.l.runner.mu.Unlock()
		return nil
	}
	server.mu.Lock()
	if server.starting != nil || server.current != nil {
		server.mu.Unlock()
		c.l.runner.mu.Unlock()
		return nil
	}
	owner := server.startOwner
	if server.startOwnerGeneration != latest.Generation {
		owner = nil
	}
	server.startOwner = nil
	server.startOwnerGeneration = 0
	server.startGeneration = latest.Generation
	server.mu.Unlock()
	c.l.startHostOperation(&server.mu, &server.starting, codexAppServerControllerName, func(ctx context.Context) error {
		return server.start(ctx, owner)
	})
	c.l.runner.mu.Unlock()
	return nil
}

func (codexAppServerInputSpec) resourceType() resource.Type             { return codexAppServerInputType }
func (value codexAppServerInputSpec) DeepCopy() codexAppServerInputSpec { return value }

func (codexAppServerRuntimeSpec) resourceType() resource.Type               { return codexAppServerRuntimeType }
func (value codexAppServerRuntimeSpec) DeepCopy() codexAppServerRuntimeSpec { return value }

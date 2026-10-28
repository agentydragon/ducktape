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
	codexInputType      = "CodexInputs.agentplane"
	codexRuntimeType    = "CodexRuntimes.agentplane"
	codexControllerName = "codex-lifecycle"
)

type codexInputSpec struct {
	Demand         uint64
	ExitGeneration uint64
	ExitProcessID  string
	ExitError      string
}

type codexPhase uint8

const (
	codexStopped codexPhase = iota
	codexStarting
	codexReady
	codexStopping
)

type codexRuntimeSpec struct {
	Phase                 codexPhase
	Generation            uint64
	Attempt               uint64
	ExitGeneration        uint64
	ExecutorReadyDeadline time.Time
	ProcessID             string
	Error                 string
	StopError             string
}

const codexExecutorReadyBudget = 30 * time.Second

type codexLifecycleController struct{ l *lifecycle }

func (*codexLifecycleController) Name() string { return codexControllerName }

func (*codexLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycleNamespace, Type: codexInputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycleNamespace, Type: runnerGoalType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerInputType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: executorRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: operationResultType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: codexRuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*codexLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() == operationResultType {
		result, found, err := lifecycleRead[operationResultSpec](ctx, r, ptr.ID())
		if err != nil || !found || result.Session != "" || result.Controller != codexControllerName {
			return nil, err
		}
	}
	return []resource.Pointer{resource.NewMetadata(lifecycleNamespace, codexInputType, "codex", resource.VersionUndefined)}, nil
}

func (c *codexLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	input, found, err := lifecycleRead[codexInputSpec](ctx, r, "codex")
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycleRead[codexRuntimeSpec](ctx, r, "codex")
	if err != nil {
		return err
	}
	goal, _, err := lifecycleRead[runnerGoalSpec](ctx, r, "runner")
	if err != nil {
		return err
	}
	outer, _, err := lifecycleRead[runnerRuntimeSpec](ctx, r, "runner")
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, "codex", current)
		if changed {
			c.l.runner.changed()
		}
		return err
	}
	server := c.l.runner.codex
	if server == nil {
		current = codexRuntimeSpec{Phase: codexStopped}
		return write()
	}

	// Consume completed native effects before making any policy decision. The slot remains owned until
	// the result is observed here, so a second start or stop cannot overlap an uncertain operation.
	if op, done := c.l.takeHostOperation(&server.mu, &server.starting); done {
		if op.err != nil {
			current.Phase = codexStopped
			current.ProcessID = ""
			current.Error = op.err.Error()
			current.ExecutorReadyDeadline = time.Time{}
		} else {
			server.mu.Lock()
			p := server.current
			generation := server.startGeneration
			server.startOwner = nil
			server.startOwnerGeneration = 0
			live := p != nil && p.process.Running()
			server.mu.Unlock()
			if live && goal.Mode == runnerRunning && generation == goal.Generation {
				current.Phase = codexReady
				current.ProcessID = p.id
				current.Attempt = input.Demand
				current.Error = ""
				current.StopError = ""
				current.ExecutorReadyDeadline = time.Time{}
			} else if live {
				// A start admitted under an older RunnerGoal may finish physically, but cannot publish
				// readiness. Shutdown will stop it after every Session has halted.
				current.Phase = codexStarting
				current.ProcessID = p.id
			} else {
				current.Phase = codexStopped
				current.ProcessID = ""
				current.Error = "Codex app-server exited before initialization completed"
			}
		}
	}
	if op, done := c.l.takeHostOperation(&server.mu, &server.stopping); done {
		current.Phase = codexStopped
		current.ProcessID = ""
		current.StopError = ""
		current.ExecutorReadyDeadline = time.Time{}
		if op.err != nil {
			current.StopError = op.err.Error()
		}
	}
	var starting, stopping *operation
	var p *codexProcess
	server.mu.Lock()
	starting = server.starting
	stopping = server.stopping
	server.mu.Unlock()
	if input.ExitGeneration > current.ExitGeneration {
		current.ExitGeneration = input.ExitGeneration
		if current.ProcessID == input.ExitProcessID && starting == nil && stopping == nil {
			current.Phase = codexStopped
			current.ProcessID = ""
			current.Error = input.ExitError
			if current.Error == "" {
				current.Error = "Codex app-server exited"
			} else if goal.Mode != runnerRunning {
				current.StopError = input.ExitError
			}
		}
	}
	if goal.Mode == runnerRunning && current.Generation != goal.Generation {
		server.mu.Lock()
		inactive := server.starting == nil && server.stopping == nil && server.current == nil
		if inactive && server.startOwnerGeneration != goal.Generation {
			server.startOwner = nil
			server.startOwnerGeneration = 0
		}
		server.mu.Unlock()
		if inactive {
			// Demand and attempts are scoped to a Runner incarnation. A resumed Runner must wait for
			// fresh demand rather than replaying the previous incarnation's Codex startup request.
			current.Generation = goal.Generation
			current.Attempt = 0
			current.ExitGeneration = input.ExitGeneration
			current.Error = ""
			current.StopError = ""
			current.ExecutorReadyDeadline = time.Time{}
			current.Phase = codexStopped
			current.ProcessID = ""
		}
	}

	if goal.Mode != runnerRunning {
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
			current.Phase = codexStopping
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
			current.Phase = codexStopped
			current.ProcessID = ""
			return write()
		}
		current.Phase = codexStopping
		current.ProcessID = p.id
		if err := write(); err != nil {
			return err
		}
		c.l.startHostOperation(&server.mu, &server.stopping, codexControllerName, server.stopProcess)
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
			if current.Phase == codexReady && current.ProcessID == p.id {
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
		current.Phase = codexStopped
		current.ProcessID = ""
		return write()
	}

	if c.l.runner.executor != nil {
		executor, found, err := lifecycleRead[executorRuntimeSpec](ctx, r, "executor")
		if err != nil {
			return err
		}
		if found && executor.Refusal != "" {
			current.Phase = codexStopped
			current.Attempt = input.Demand
			current.Error = fmt.Sprintf("Codex executor startup refused: %s", executor.Refusal)
			current.ProcessID = ""
			current.ExecutorReadyDeadline = time.Time{}
			return write()
		}
		if found && executor.StopError != "" {
			current.Phase = codexStopped
			current.Attempt = input.Demand
			current.Error = fmt.Sprintf("Codex executor stopped with error: %s", executor.StopError)
			current.ProcessID = ""
			current.ExecutorReadyDeadline = time.Time{}
			return write()
		}
		now := time.Now()
		if !current.ExecutorReadyDeadline.IsZero() && !now.Before(current.ExecutorReadyDeadline) {
			current.Phase = codexStopped
			current.Attempt = input.Demand
			current.Error = fmt.Sprintf("Codex executor did not become ready within %s", codexExecutorReadyBudget)
			current.ExecutorReadyDeadline = time.Time{}
			return write()
		}
		if !found || executor.Phase != executorReady {
			current.Generation = goal.Generation
			current.Phase = codexStarting
			current.ProcessID = ""
			if current.ExecutorReadyDeadline.IsZero() {
				current.Error = ""
				current.ExecutorReadyDeadline = now.Add(codexExecutorReadyBudget)
				if err := write(); err != nil {
					return err
				}
				return controller.NewRequeueError(nil, codexExecutorReadyBudget)
			}
			return controller.NewRequeueError(nil, current.ExecutorReadyDeadline.Sub(now))
		}
		current.ExecutorReadyDeadline = time.Time{}
	}

	current.Phase = codexStarting
	current.Generation = goal.Generation
	current.Attempt = input.Demand
	current.ExecutorReadyDeadline = time.Time{}
	current.ProcessID = ""
	current.Error = ""
	if err := write(); err != nil {
		return err
	}

	// Runner.mu is the synchronous admission barrier shared with Close and Quiesce. Once the slot is
	// claimed under that barrier, a concurrent goal change waits until the native effect is admitted.
	c.l.runner.mu.Lock()
	latest := c.l.goal()
	if latest != goal || latest.Mode != runnerRunning {
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
	c.l.startHostOperation(&server.mu, &server.starting, codexControllerName, func(ctx context.Context) error {
		return server.start(ctx, owner)
	})
	c.l.runner.mu.Unlock()
	return nil
}

func (codexInputSpec) resourceType() resource.Type    { return codexInputType }
func (value codexInputSpec) DeepCopy() codexInputSpec { return value }

func (codexRuntimeSpec) resourceType() resource.Type      { return codexRuntimeType }
func (value codexRuntimeSpec) DeepCopy() codexRuntimeSpec { return value }

package runner

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const (
	codexExecServerInputType      = "CodexExecServerInputs.agentplane"
	codexExecServerRuntimeType    = "CodexExecServerRuntimes.agentplane"
	codexExecServerControllerName = "codex-exec-server-lifecycle"
)

type codexExecServerInputSpec struct {
	Demand         uint64
	ExitGeneration uint64
	ExitError      string
}

type codexExecServerPhase uint8

const (
	codexExecServerStopped codexExecServerPhase = iota
	codexExecServerStarting
	codexExecServerProbing
	codexExecServerReady
	codexExecServerStopping
	codexExecServerRetrying
)

type codexExecServerRuntimeSpec struct {
	Phase                    codexExecServerPhase
	Generation               uint64
	FencedGeneration         uint64
	Error                    string
	StopError                string
	Refusal                  string
	RestartFailures          uint32
	ConsecutiveProbeFailures uint8
	ReadyDeadline            time.Time
	NextProbe                time.Time
	RetryAt                  time.Time
}

const (
	codexExecServerReadyBudget    = 20 * time.Second
	codexExecServerReadinessRetry = 100 * time.Millisecond
	codexExecServerReadinessProbe = 2 * time.Second
	codexExecServerHealthInterval = 5 * time.Second
	codexExecServerHealthDeadline = 3 * time.Second
	codexExecServerFenceDeadline  = 90 * time.Second
	codexExecServerResourceID     = "codex-exec-server"
)

type codexExecServerLifecycleController struct {
	l    *lifecycle
	host *codexExecServer
}

func (*codexExecServerLifecycleController) Name() string { return codexExecServerControllerName }

func (*codexExecServerLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycleNamespace, Type: codexExecServerInputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycleNamespace, Type: runnerDesiredStateType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerInputType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: operationResultType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: codexExecServerRuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (c *codexExecServerLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	input := resource.NewMetadata(lifecycleNamespace, codexExecServerInputType, codexExecServerResourceID, resource.VersionUndefined)
	switch ptr.Type() {
	case codexExecServerInputType, runnerDesiredStateType, runnerRuntimeType, runnerInputType:
		return []resource.Pointer{input}, nil
	case operationResultType:
		result, found, err := lifecycleRead[operationResultSpec](ctx, r, ptr.ID())
		if err != nil || !found || result.Controller != codexExecServerControllerName {
			return nil, err
		}
		return []resource.Pointer{input}, nil
	default:
		return nil, nil
	}
}

func (c *codexExecServerLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	e := c.host
	if e == nil {
		return nil
	}
	input, found, err := lifecycleRead[codexExecServerInputSpec](ctx, r, ptr.ID())
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycleRead[codexExecServerRuntimeSpec](ctx, r, ptr.ID())
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
		changed, err := lifecycleWrite(ctx, r, codexExecServerResourceID, current)
		if changed {
			c.l.runner.changed()
		}
		return err
	}

	startResult, startDone := c.l.takeHostOperation(&e.mu, &e.startOp)
	probeResult, probeDone := c.l.takeHostOperation(&e.mu, &e.probeOp)
	fenceResult, fenceDone := c.l.takeHostOperation(&e.mu, &e.fenceOp)
	instance, instanceGeneration := e.currentInstance()
	now := time.Now()
	shuttingDown := desiredState.Mode != runnerRunning && outer.Stage >= stageFencing

	if startDone && current.Phase == codexExecServerStarting {
		if startResult.err != nil {
			var refusal codexExecServerRefusal
			if errors.As(startResult.err, &refusal) {
				if current.Refusal != refusal.reason {
					zap.L().Error("Codex exec-server refused to start", zap.Error(refusal))
				}
				current.Refusal = refusal.reason
			} else {
				current.Refusal = ""
			}
			current.Error = startResult.err.Error()
			current.RestartFailures++
			if instance != nil && instanceGeneration == current.Generation {
				current.Phase = codexExecServerStopping
			} else {
				current.Phase = codexExecServerRetrying
				current.RetryAt = now.Add(codexExecServerRestartDelay(current.RestartFailures))
			}
		} else {
			current.Refusal = ""
			current.Error = ""
			current.Phase = codexExecServerProbing
			current.NextProbe = now
		}
	}

	if probeDone {
		switch current.Phase {
		case codexExecServerProbing:
			if probeResult.err == nil {
				current.Phase = codexExecServerReady
				current.Error = ""
				current.Refusal = ""
				current.RestartFailures = 0
				current.ConsecutiveProbeFailures = 0
				current.NextProbe = now.Add(codexExecServerHealthInterval)
				zap.L().Info("Codex exec-server ready", zap.String("url", e.config.URL))
			} else if !now.Before(current.ReadyDeadline) {
				current.Error = fmt.Sprintf("Codex exec-server did not become ready within %s: %v", codexExecServerReadyBudget, probeResult.err)
				current.RestartFailures++
				current.Phase = codexExecServerStopping
			} else {
				current.Error = probeResult.err.Error()
				current.NextProbe = now.Add(codexExecServerReadinessRetry)
			}
		case codexExecServerReady:
			if probeResult.err == nil {
				current.ConsecutiveProbeFailures = 0
				current.Error = ""
				current.RestartFailures = 0
				current.NextProbe = now.Add(codexExecServerHealthInterval)
			} else {
				current.ConsecutiveProbeFailures++
				current.Error = probeResult.err.Error()
				zap.L().Warn("Codex exec-server health probe failed", zap.Int("consecutive_failures", int(current.ConsecutiveProbeFailures)), zap.Error(probeResult.err))
				if current.ConsecutiveProbeFailures >= 3 {
					current.Error = fmt.Sprintf("executor failed %d consecutive health probes: %v", current.ConsecutiveProbeFailures, probeResult.err)
					current.RestartFailures++
					current.Phase = codexExecServerStopping
				} else {
					current.NextProbe = now.Add(codexExecServerHealthInterval)
				}
			}
		}
	}

	if fenceDone {
		if fenceResult.err != nil {
			fatal := fmt.Errorf("Codex exec-server fencing failed: %w", fenceResult.err)
			current.Phase = codexExecServerStopped
			current.Error = fatal.Error()
			current.StopError = fatal.Error()
			current.RetryAt = time.Time{}
			if err := write(); err != nil {
				return err
			}
			c.l.runner.fail(fatal)
			return fatal
		}
		e.clearInstance(instance)
		current.FencedGeneration = current.Generation
		instance = nil
		if shuttingDown || desiredState.Mode != runnerRunning {
			current.Phase = codexExecServerStopped
			current.Error = ""
			current.Refusal = ""
			current.RetryAt = time.Time{}
		} else {
			current.Phase = codexExecServerRetrying
			current.RetryAt = now.Add(codexExecServerRestartDelay(current.RestartFailures))
			zap.L().Warn("Codex exec-server lost; restarting without replay", zap.String("error", current.Error))
		}
	}
	if startDone || probeDone || fenceDone {
		if err := write(); err != nil {
			return err
		}
	}

	// The exit observer publishes only a fact. Reconciliation decides whether to
	// fence and whether the outer Runner state permits a successor.
	if input.ExitGeneration == current.Generation && current.Generation != 0 && current.FencedGeneration != current.Generation && current.Phase != codexExecServerStopping && current.Phase != codexExecServerStopped {
		current.Error = input.ExitError
		if current.Error == "" {
			current.Error = "Codex exec-server exited"
		}
		current.RestartFailures++
		current.Phase = codexExecServerStopping
		current.RetryAt = time.Time{}
	}

	if shuttingDown {
		if current.Phase != codexExecServerStopped && current.Phase != codexExecServerStopping {
			current.Phase = codexExecServerStopping
			current.Error = ""
		}
		if current.Phase == codexExecServerStopping {
			if err := c.reconcileStop(ctx, e, instance, instanceGeneration, &current, write); err != nil {
				return err
			}
		}
		return write()
	}
	if current.StopError != "" {
		// A failed fence leaves ownership uncertain. Retain the instance and block
		// all successor starts until the Runner reports the fatal stop error.
		return nil
	}

	// Quiescing and closing suppress retries immediately, but a healthy executor
	// remains available until the outer lifecycle reaches its fencing barrier.
	if desiredState.Mode != runnerRunning && current.Phase == codexExecServerRetrying && instance == nil {
		current.Phase = codexExecServerStopped
		current.RetryAt = time.Time{}
		return write()
	}

	if current.Phase == codexExecServerStopping {
		if err := c.reconcileStop(ctx, e, instance, instanceGeneration, &current, write); err != nil {
			return err
		}
		return write()
	}

	if current.Phase == codexExecServerStarting {
		return nil
	}
	if current.Phase == codexExecServerProbing {
		if !now.Before(current.ReadyDeadline) {
			current.Error = "Codex exec-server did not become ready before the readiness deadline"
			current.RestartFailures++
			current.Phase = codexExecServerStopping
			if err := write(); err != nil {
				return err
			}
			c.cancelHostOperation(&e.probeOp)
			return nil
		}
		if probeResult != nil {
			return controller.NewRequeueError(nil, codexExecServerReadinessRetry)
		}
		if probePending := c.hostOperationPending(&e.probeOp); probePending {
			return nil
		}
		if now.Before(current.NextProbe) {
			return controller.NewRequeueError(nil, current.NextProbe.Sub(now))
		}
		if err := write(); err != nil {
			return err
		}
		c.l.startHostOperation(&e.mu, &e.probeOp, codexExecServerControllerName, func(ctx context.Context) error {
			probeCtx, cancel := context.WithDeadline(ctx, current.ReadyDeadline)
			defer cancel()
			probeCtx, cancel = context.WithTimeout(probeCtx, codexExecServerReadinessProbe)
			defer cancel()
			return e.probe(probeCtx)
		})
		return nil
	}

	if current.Phase == codexExecServerReady {
		if input.ExitGeneration == current.Generation {
			return nil
		}
		if c.hostOperationPending(&e.probeOp) {
			return nil
		}
		if now.Before(current.NextProbe) {
			return controller.NewRequeueError(nil, current.NextProbe.Sub(now))
		}
		c.l.startHostOperation(&e.mu, &e.probeOp, codexExecServerControllerName, func(ctx context.Context) error {
			probeCtx, cancel := context.WithTimeout(ctx, codexExecServerHealthDeadline)
			defer cancel()
			return e.probe(probeCtx)
		})
		return nil
	}

	if current.Phase == codexExecServerRetrying {
		if desiredState.Mode != runnerRunning || input.Demand == 0 {
			current.Phase = codexExecServerStopped
			current.RetryAt = time.Time{}
			return write()
		}
		if now.Before(current.RetryAt) {
			return controller.NewRequeueError(nil, current.RetryAt.Sub(now))
		}
	}

	if current.Phase == codexExecServerStopped || current.Phase == codexExecServerRetrying {
		if desiredState.Mode != runnerRunning || input.Demand == 0 {
			return nil
		}
		current.Phase = codexExecServerStarting
		current.Generation++
		current.ReadyDeadline = now.Add(codexExecServerReadyBudget)
		current.NextProbe = time.Time{}
		current.RetryAt = time.Time{}
		current.Error = ""
		current.Refusal = ""
		if err := write(); err != nil {
			return err
		}
		generation := current.Generation
		c.l.runner.mu.Lock()
		latest := c.l.desiredState()
		if latest != desiredState || latest.Mode != runnerRunning {
			c.l.runner.mu.Unlock()
			current.Phase = codexExecServerStopped
			current.ReadyDeadline = time.Time{}
			current.NextProbe = time.Time{}
			return write()
		}
		c.l.startHostOperation(&e.mu, &e.startOp, codexExecServerControllerName, func(ctx context.Context) error {
			startCtx, cancel := context.WithDeadline(ctx, current.ReadyDeadline)
			defer cancel()
			instance, err := e.startProcess(startCtx)
			if err != nil {
				return err
			}
			if instance == nil {
				return errors.New("executor start returned no process")
			}
			e.install(instance, generation)
			return nil
		})
		c.l.runner.mu.Unlock()
		return nil
	}

	return write()
}

func (c *codexExecServerLifecycleController) reconcileStop(ctx context.Context, e *codexExecServer, instance *codexExecServerInstance, generation uint64, current *codexExecServerRuntimeSpec, write func() error) error {
	c.cancelHostOperation(&e.startOp)
	c.cancelHostOperation(&e.probeOp)
	if c.hostOperationPending(&e.startOp) || c.hostOperationPending(&e.probeOp) {
		return write()
	}
	if c.hostOperationPending(&e.fenceOp) {
		return write()
	}
	if instance == nil {
		if desiredState := c.l.desiredState(); desiredState.Mode != runnerRunning {
			current.Phase = codexExecServerStopped
			current.Error = ""
			current.Refusal = ""
			current.RetryAt = time.Time{}
			return write()
		}
		current.Phase = codexExecServerRetrying
		current.RetryAt = time.Now().Add(codexExecServerRestartDelay(current.RestartFailures))
		return write()
	}
	c.l.startHostOperation(&e.mu, &e.fenceOp, codexExecServerControllerName, func(ctx context.Context) error {
		fenceCtx, cancel := context.WithTimeout(ctx, codexExecServerFenceDeadline)
		defer cancel()
		return instance.stop(fenceCtx)
	})
	return write()
}

func (c *codexExecServerLifecycleController) hostOperationPending(slot **operation) bool {
	c.host.mu.Lock()
	op := *slot
	c.host.mu.Unlock()
	return op != nil
}

func (c *codexExecServerLifecycleController) cancelHostOperation(slot **operation) {
	c.host.mu.Lock()
	op := *slot
	c.host.mu.Unlock()
	if op != nil && op.cancel != nil {
		op.cancel()
	}
}

func codexExecServerRestartDelay(failures uint32) time.Duration {
	if failures == 0 {
		failures = 1
	}
	if failures >= 5 {
		return 10 * time.Second
	}
	seconds := uint64(1) << (failures - 1)
	if seconds > 10 {
		seconds = 10
	}
	return time.Duration(seconds) * time.Second
}

func (codexExecServerInputSpec) resourceType() resource.Type              { return codexExecServerInputType }
func (value codexExecServerInputSpec) DeepCopy() codexExecServerInputSpec { return value }

func (codexExecServerRuntimeSpec) resourceType() resource.Type                { return codexExecServerRuntimeType }
func (value codexExecServerRuntimeSpec) DeepCopy() codexExecServerRuntimeSpec { return value }

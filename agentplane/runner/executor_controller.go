package runner

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const (
	executorInputType      = "ExecutorInputs.agentplane"
	executorRuntimeType    = "ExecutorRuntimes.agentplane"
	executorControllerName = "executor-lifecycle"
)

type executorInputSpec struct {
	Demand         uint64
	Revision       uint64
	ExitGeneration uint64
	ExitError      string
}

type executorPhase uint8

const (
	executorStopped executorPhase = iota
	executorStarting
	executorProbing
	executorReady
	executorStopping
	executorRetrying
)

type executorRuntimeSpec struct {
	Phase                    executorPhase
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
	executorReadyBudget     = 20 * time.Second
	executorReadinessRetry  = 100 * time.Millisecond
	executorReadinessProbe  = 2 * time.Second
	executorHealthInterval  = 5 * time.Second
	executorHealthDeadline  = 3 * time.Second
	executorFenceDeadline   = 90 * time.Second
	executorControllerInput = "executor"
)

type executorLifecycleController struct{ l *lifecycle }

func (*executorLifecycleController) Name() string { return executorControllerName }

func (*executorLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycleNamespace, Type: executorInputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycleNamespace, Type: runnerGoalType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerInputType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: operationResultType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: executorRuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (c *executorLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	input := resource.NewMetadata(lifecycleNamespace, executorInputType, executorControllerInput, resource.VersionUndefined)
	switch ptr.Type() {
	case executorInputType, runnerGoalType, runnerRuntimeType, runnerInputType:
		return []resource.Pointer{input}, nil
	case operationResultType:
		result, found, err := lifecycleRead[operationResultSpec](ctx, r, operationResultType, ptr.ID())
		if err != nil || !found || !strings.HasPrefix(result.Kind, "executor-") {
			return nil, err
		}
		return []resource.Pointer{input}, nil
	default:
		return nil, nil
	}
}

func (c *executorLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	e := c.l.runner.executor
	if e == nil {
		return nil
	}
	input, found, err := lifecycleRead[executorInputSpec](ctx, r, executorInputType, ptr.ID())
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycleRead[executorRuntimeSpec](ctx, r, executorRuntimeType, ptr.ID())
	if err != nil {
		return err
	}
	goal, _, err := lifecycleRead[runnerGoalSpec](ctx, r, runnerGoalType, "runner")
	if err != nil {
		return err
	}
	outer, _, err := lifecycleRead[runnerRuntimeSpec](ctx, r, runnerRuntimeType, "runner")
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, executorRuntimeType, executorControllerInput, current)
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
	shuttingDown := goal.Mode != runnerRunning && outer.Stage >= stageFencing

	if startDone && current.Phase == executorStarting {
		if startResult.err != nil {
			var refusal executorRefusal
			if errors.As(startResult.err, &refusal) {
				if current.Refusal != refusal.reason {
					zap.L().Error("Codex executor refused to start", zap.Error(refusal))
				}
				current.Refusal = refusal.reason
			} else {
				current.Refusal = ""
			}
			current.Error = startResult.err.Error()
			current.RestartFailures++
			if instance != nil && instanceGeneration == current.Generation {
				current.Phase = executorStopping
			} else {
				current.Phase = executorRetrying
				current.RetryAt = now.Add(executorRestartDelay(current.RestartFailures))
			}
		} else {
			current.Refusal = ""
			current.Error = ""
			current.Phase = executorProbing
			current.NextProbe = now
		}
	}

	if probeDone {
		switch current.Phase {
		case executorProbing:
			if probeResult.err == nil {
				current.Phase = executorReady
				current.Error = ""
				current.Refusal = ""
				current.RestartFailures = 0
				current.ConsecutiveProbeFailures = 0
				current.NextProbe = now.Add(executorHealthInterval)
				zap.L().Info("Codex executor ready", zap.String("url", e.config.URL))
			} else if !now.Before(current.ReadyDeadline) {
				current.Error = fmt.Sprintf("Codex executor did not become ready within %s: %v", executorReadyBudget, probeResult.err)
				current.RestartFailures++
				current.Phase = executorStopping
			} else {
				current.Error = probeResult.err.Error()
				current.NextProbe = now.Add(executorReadinessRetry)
			}
		case executorReady:
			if probeResult.err == nil {
				current.ConsecutiveProbeFailures = 0
				current.Error = ""
				current.RestartFailures = 0
				current.NextProbe = now.Add(executorHealthInterval)
			} else {
				current.ConsecutiveProbeFailures++
				current.Error = probeResult.err.Error()
				zap.L().Warn("Codex executor health probe failed", zap.Int("consecutive_failures", int(current.ConsecutiveProbeFailures)), zap.Error(probeResult.err))
				if current.ConsecutiveProbeFailures >= 3 {
					current.Error = fmt.Sprintf("executor failed %d consecutive health probes: %v", current.ConsecutiveProbeFailures, probeResult.err)
					current.RestartFailures++
					current.Phase = executorStopping
				} else {
					current.NextProbe = now.Add(executorHealthInterval)
				}
			}
		}
	}

	if fenceDone {
		if fenceResult.err != nil {
			fatal := fmt.Errorf("Codex executor fencing failed: %w", fenceResult.err)
			current.Phase = executorStopped
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
		if shuttingDown || goal.Mode != runnerRunning {
			current.Phase = executorStopped
			current.Error = ""
			current.Refusal = ""
			current.RetryAt = time.Time{}
		} else {
			current.Phase = executorRetrying
			current.RetryAt = now.Add(executorRestartDelay(current.RestartFailures))
			zap.L().Warn("Codex executor lost; restarting without replay", zap.String("error", current.Error))
		}
	}
	if startDone || probeDone || fenceDone {
		if err := write(); err != nil {
			return err
		}
	}

	// The exit observer publishes only a fact. Reconciliation decides whether to
	// fence and whether the outer Runner state permits a successor.
	if input.ExitGeneration == current.Generation && current.Generation != 0 && current.FencedGeneration != current.Generation && current.Phase != executorStopping && current.Phase != executorStopped {
		current.Error = input.ExitError
		if current.Error == "" {
			current.Error = "Codex executor exited"
		}
		current.RestartFailures++
		current.Phase = executorStopping
		current.RetryAt = time.Time{}
	}

	if shuttingDown {
		if current.Phase != executorStopped && current.Phase != executorStopping {
			current.Phase = executorStopping
			current.Error = ""
		}
		if current.Phase == executorStopping {
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
	if goal.Mode != runnerRunning && current.Phase == executorRetrying && instance == nil {
		current.Phase = executorStopped
		current.RetryAt = time.Time{}
		return write()
	}

	if current.Phase == executorStopping {
		if err := c.reconcileStop(ctx, e, instance, instanceGeneration, &current, write); err != nil {
			return err
		}
		return write()
	}

	if current.Phase == executorStarting {
		return nil
	}
	if current.Phase == executorProbing {
		if !now.Before(current.ReadyDeadline) {
			current.Error = "Codex executor did not become ready before the readiness deadline"
			current.RestartFailures++
			current.Phase = executorStopping
			if err := write(); err != nil {
				return err
			}
			c.cancelHostOperation(&e.probeOp)
			return nil
		}
		if probeResult != nil {
			return controller.NewRequeueError(nil, executorReadinessRetry)
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
		c.l.startHostOperation(&e.mu, &e.probeOp, "executor-readiness-probe", func(ctx context.Context) error {
			probeCtx, cancel := context.WithDeadline(ctx, current.ReadyDeadline)
			defer cancel()
			probeCtx, cancel = context.WithTimeout(probeCtx, executorReadinessProbe)
			defer cancel()
			return e.probe(probeCtx)
		})
		return nil
	}

	if current.Phase == executorReady {
		if input.ExitGeneration == current.Generation {
			return nil
		}
		if c.hostOperationPending(&e.probeOp) {
			return nil
		}
		if now.Before(current.NextProbe) {
			return controller.NewRequeueError(nil, current.NextProbe.Sub(now))
		}
		c.l.startHostOperation(&e.mu, &e.probeOp, "executor-health-probe", func(ctx context.Context) error {
			probeCtx, cancel := context.WithTimeout(ctx, executorHealthDeadline)
			defer cancel()
			return e.probe(probeCtx)
		})
		return nil
	}

	if current.Phase == executorRetrying {
		if goal.Mode != runnerRunning || input.Demand == 0 {
			current.Phase = executorStopped
			current.RetryAt = time.Time{}
			return write()
		}
		if now.Before(current.RetryAt) {
			return controller.NewRequeueError(nil, current.RetryAt.Sub(now))
		}
	}

	if current.Phase == executorStopped || current.Phase == executorRetrying {
		if goal.Mode != runnerRunning || input.Demand == 0 {
			return nil
		}
		current.Phase = executorStarting
		current.Generation++
		current.ReadyDeadline = now.Add(executorReadyBudget)
		current.NextProbe = time.Time{}
		current.RetryAt = time.Time{}
		current.Error = ""
		current.Refusal = ""
		if err := write(); err != nil {
			return err
		}
		generation := current.Generation
		c.l.runner.mu.Lock()
		latest := c.l.goal()
		if latest != goal || latest.Mode != runnerRunning {
			c.l.runner.mu.Unlock()
			current.Phase = executorStopped
			current.ReadyDeadline = time.Time{}
			current.NextProbe = time.Time{}
			return write()
		}
		c.l.startHostOperation(&e.mu, &e.startOp, "executor-start", func(ctx context.Context) error {
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

func (c *executorLifecycleController) reconcileStop(ctx context.Context, e *codexExecutor, instance *executorInstance, generation uint64, current *executorRuntimeSpec, write func() error) error {
	c.cancelHostOperation(&e.startOp)
	c.cancelHostOperation(&e.probeOp)
	if c.hostOperationPending(&e.startOp) || c.hostOperationPending(&e.probeOp) {
		return write()
	}
	if c.hostOperationPending(&e.fenceOp) {
		return write()
	}
	if instance == nil {
		if goal := c.l.goal(); goal.Mode != runnerRunning {
			current.Phase = executorStopped
			current.Error = ""
			current.Refusal = ""
			current.RetryAt = time.Time{}
			return write()
		}
		current.Phase = executorRetrying
		current.RetryAt = time.Now().Add(executorRestartDelay(current.RestartFailures))
		return write()
	}
	c.l.startHostOperation(&e.mu, &e.fenceOp, "executor-fence", func(ctx context.Context) error {
		fenceCtx, cancel := context.WithTimeout(ctx, executorFenceDeadline)
		defer cancel()
		return instance.stop(fenceCtx)
	})
	return write()
}

func (c *executorLifecycleController) hostOperationPending(slot **sessionOperation) bool {
	c.l.runner.executor.mu.Lock()
	op := *slot
	c.l.runner.executor.mu.Unlock()
	return op != nil
}

func (c *executorLifecycleController) cancelHostOperation(slot **sessionOperation) {
	c.l.runner.executor.mu.Lock()
	op := *slot
	c.l.runner.executor.mu.Unlock()
	if op != nil && op.cancel != nil {
		op.cancel()
	}
}

func executorRestartDelay(failures uint32) time.Duration {
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

package runner

import (
	"context"
	"fmt"
	"time"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const (
	codexExecServerInputType      = "CodexExecServerInputs.agentplane"
	codexExecServerRuntimeType    = "CodexExecServerRuntimes.agentplane"
	codexExecServerControllerName = "codex-exec-server-lifecycle"
)

type codexExecServerInputSpec struct{ Demand uint64 }

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
	ProcessID                string
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
			{Namespace: lifecycleNamespace, Type: processDesiredStateType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: processStatusType, Kind: controller.InputQMapped},
		},
		Outputs: []controller.Output{
			{Type: codexExecServerRuntimeType, Kind: controller.OutputExclusive},
			{Type: processDesiredStateType, Kind: controller.OutputShared},
		},
		Concurrency: optional.Some(uint(1)),
	}
}

func (c *codexExecServerLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	input := resource.NewMetadata(lifecycleNamespace, codexExecServerInputType, codexExecServerResourceID, resource.VersionUndefined)
	switch ptr.Type() {
	case codexExecServerInputType, runnerDesiredStateType, runnerRuntimeType, runnerInputType:
		return []resource.Pointer{input}, nil
	case processDesiredStateType, processStatusType:
		var owner string
		var found bool
		var err error
		if ptr.Type() == processDesiredStateType {
			var desired processDesiredStateSpec
			desired, found, err = lifecycleRead[processDesiredStateSpec](ctx, r, ptr.ID())
			owner = desired.Owner
		} else {
			var status processStatusSpec
			status, found, err = lifecycleRead[processStatusSpec](ctx, r, ptr.ID())
			owner = status.Owner
		}
		if err != nil {
			return nil, err
		}
		if !found {
			current, _, err := lifecycleRead[codexExecServerRuntimeSpec](ctx, r, codexExecServerResourceID)
			if err != nil {
				return nil, err
			}
			if current.ProcessID == ptr.ID() {
				owner = codexExecServerControllerName
			}
		}
		if owner != codexExecServerControllerName {
			return nil, nil
		}
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

	probeResult, probeDone := c.l.takeHostOperation(&e.mu, &e.probeOp)
	var process processStatusSpec
	var processFound bool
	if current.ProcessID != "" {
		process, processFound, err = lifecycleRead[processStatusSpec](ctx, r, current.ProcessID)
		if err != nil {
			return err
		}
	}
	now := time.Now()
	shuttingDown := desiredState.Mode != runnerRunning && outer.Stage >= stageFencing

	if process.StopError != "" {
		c.cancelHostOperation(&e.probeOp)
		fatal := fmt.Errorf("Codex exec-server fencing failed: %s", process.StopError)
		current.Phase = codexExecServerStopped
		current.Error, current.StopError = fatal.Error(), fatal.Error()
		current.RetryAt = time.Time{}
		if err := write(); err != nil {
			return err
		}
		c.l.runner.fail(fatal)
		return fatal
	}
	if current.Phase == codexExecServerStarting && processFound {
		if process.Phase == processRunning {
			current.Refusal, current.Error = "", ""
			current.Phase = codexExecServerProbing
			current.NextProbe = now
		} else if process.Phase == processExited || process.Phase == processStopping {
			current.Refusal = process.StartRefusal
			current.Error = process.StartError
			if current.Error == "" {
				current.Error = process.ExitError
			}
			if current.Error == "" {
				current.Error = "Codex exec-server exited during startup"
			}
			current.RestartFailures++
			current.Phase = codexExecServerStopping
		}
		if err := write(); err != nil {
			return err
		}
	}
	if (current.Phase == codexExecServerProbing || current.Phase == codexExecServerReady) && processFound && process.Phase != processRunning {
		current.Error = process.ExitError
		if current.Error == "" {
			current.Error = "Codex exec-server exited"
		}
		current.RestartFailures++
		current.Phase = codexExecServerStopping
		current.RetryAt = time.Time{}
		if err := write(); err != nil {
			return err
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

	if probeDone {
		if err := write(); err != nil {
			return err
		}
	}

	if shuttingDown {
		if current.Phase != codexExecServerStopped && current.Phase != codexExecServerStopping {
			current.Phase = codexExecServerStopping
			current.Error = ""
		}
		if current.Phase == codexExecServerStopping {
			if err := c.reconcileStop(ctx, r, e, &current, write); err != nil {
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
	if desiredState.Mode != runnerRunning && current.Phase == codexExecServerRetrying && current.ProcessID == "" {
		current.Phase = codexExecServerStopped
		current.RetryAt = time.Time{}
		return write()
	}

	if current.Phase == codexExecServerStopping {
		if err := c.reconcileStop(ctx, r, e, &current, write); err != nil {
			return err
		}
		return write()
	}

	if current.Phase == codexExecServerStarting {
		return c.ensureProcessIntent(ctx, r, e, desiredState, current)
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
		current.ProcessID = fmt.Sprintf("%s-%d", codexExecServerResourceID, current.Generation)
		current.ReadyDeadline = now.Add(codexExecServerReadyBudget)
		current.NextProbe = time.Time{}
		current.RetryAt = time.Time{}
		current.Error = ""
		current.Refusal = ""
		if err := write(); err != nil {
			return err
		}
		return c.ensureProcessIntent(ctx, r, e, desiredState, current)
	}

	return write()
}

func (c *codexExecServerLifecycleController) ensureProcessIntent(ctx context.Context, r controller.QRuntime, e *codexExecServer, desired runnerDesiredStateSpec, current codexExecServerRuntimeSpec) error {
	c.l.runner.mu.Lock()
	defer c.l.runner.mu.Unlock()
	latest := c.l.desiredState()
	if latest != desired || latest.Mode != runnerRunning {
		return nil
	}
	return c.l.processes.request(ctx, r, current.ProcessID, processDesiredStateSpec{
		Owner: codexExecServerControllerName, Generation: current.Generation,
		StartDeadline: current.ReadyDeadline, StopTimeout: codexExecServerFenceDeadline,
	}, e.startProcess)
}

func (c *codexExecServerLifecycleController) reconcileStop(ctx context.Context, r controller.QRuntime, e *codexExecServer, current *codexExecServerRuntimeSpec, write func() error) error {
	c.cancelHostOperation(&e.probeOp)
	if c.hostOperationPending(&e.probeOp) {
		return write()
	}
	if current.ProcessID != "" {
		ptr := resource.NewMetadata(lifecycleNamespace, processDesiredStateType, current.ProcessID, resource.VersionUndefined)
		ready, err := r.Teardown(ctx, ptr)
		if err != nil && !state.IsNotFoundError(err) {
			return err
		}
		if err == nil {
			if !ready {
				return write()
			}
			if err := r.Destroy(ctx, ptr); err != nil && !state.IsNotFoundError(err) {
				return err
			}
		}
		current.ProcessID = ""
		current.FencedGeneration = current.Generation
	}
	if desired := c.l.desiredState(); desired.Mode != runnerRunning {
		current.Phase = codexExecServerStopped
		current.Error, current.Refusal = "", ""
		current.RetryAt = time.Time{}
	} else {
		current.Phase = codexExecServerRetrying
		current.RetryAt = time.Now().Add(codexExecServerRestartDelay(current.RestartFailures))
	}
	if err := write(); err != nil {
		return err
	}
	if current.Phase == codexExecServerRetrying {
		return controller.NewRequeueError(nil, time.Until(current.RetryAt))
	}
	return nil
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

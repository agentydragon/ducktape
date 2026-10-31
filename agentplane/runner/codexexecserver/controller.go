package codexexecserver

import (
	"context"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

type LifecycleController struct{ host *Host }

func (*LifecycleController) Name() string { return ControllerName }

func (*LifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycle.Namespace, Type: InputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycle.Namespace, Type: RuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: lifecycle.DesiredStateType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: lifecycle.AggregateStatusType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: OperationType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: supervision.DesiredStateType, Kind: controller.InputQMapped},
			{Namespace: lifecycle.Namespace, Type: supervision.StatusType, Kind: controller.InputQMapped},
		},
		Outputs: []controller.Output{
			{Type: RuntimeType, Kind: controller.OutputExclusive},
			{Type: supervision.DesiredStateType, Kind: controller.OutputShared},
		},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*LifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	input := resource.NewMetadata(lifecycle.Namespace, InputType, ResourceID, resource.VersionUndefined)
	switch ptr.Type() {
	case InputType, RuntimeType, lifecycle.DesiredStateType, lifecycle.AggregateStatusType:
		return []resource.Pointer{input}, nil
	case supervision.DesiredStateType, supervision.StatusType:
		var owner string
		var found bool
		var err error
		if ptr.Type() == supervision.DesiredStateType {
			var desired supervision.DesiredState
			desired, found, err = supervision.ReadDesiredState(ctx, r, lifecycle.Namespace, ptr.ID())
			owner = desired.Owner
		} else {
			var status supervision.Status
			status, found, err = supervision.ReadStatus(ctx, r, lifecycle.Namespace, ptr.ID())
			owner = status.Owner
		}
		if err != nil {
			return nil, err
		}
		if !found {
			current, _, err := ReadRuntime(ctx, r)
			if err != nil {
				return nil, err
			}
			if current.ProcessID == ptr.ID() {
				owner = ControllerName
			}
		}
		if owner != ControllerName {
			return nil, nil
		}
		return []resource.Pointer{input}, nil
	case OperationType:
		result, found, err := readOperation(ctx, r, ptr.ID())
		if err != nil || !found || result.Controller != ControllerName {
			return nil, err
		}
		return []resource.Pointer{input}, nil
	default:
		return nil, nil
	}
}

func (c *LifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	h := c.host
	if h == nil {
		return nil
	}
	input, found, err := ReadInput(ctx, r)
	if err != nil || !found {
		return err
	}
	current, _, err := ReadRuntime(ctx, r)
	if err != nil {
		return err
	}
	desired, _, err := lifecycle.ReadDesired(ctx, r)
	if err != nil {
		return err
	}
	outer, _, err := lifecycle.ReadStatus(ctx, r)
	if err != nil {
		return err
	}
	writtenPhase := current.Phase
	write := func() error {
		changed, err := componentWrite(ctx, r, ResourceID, current)
		if err == nil {
			if changed && writtenPhase == Probing && current.Phase == Stopping {
				zap.L().Warn("Codex exec-server readiness failed", zap.String("process_id", current.ProcessID), zap.Time("deadline", current.ReadyDeadline), zap.String("error", current.Error))
			}
			writtenPhase = current.Phase
		}
		h.withRuntimeChange(changed)
		return err
	}

	probeResult, probeDone := h.takeProbe()
	var process supervision.Status
	var processFound bool
	if current.ProcessID != "" {
		process, processFound, err = supervision.ReadStatus(ctx, r, lifecycle.Namespace, current.ProcessID)
		if err != nil {
			return err
		}
	}
	now := time.Now()
	shuttingDown := desired.Mode != lifecycle.Running && outer.Stage >= lifecycle.Fencing

	if process.StopError != "" {
		h.cancelProbe()
		fatal := fmt.Errorf("Codex exec-server fencing failed: %s", process.StopError)
		current.Phase = Stopped
		current.Error, current.StopError = fatal.Error(), fatal.Error()
		current.RetryAt = time.Time{}
		if err := write(); err != nil {
			return err
		}
		h.onError(fatal)
		return fatal
	}
	if current.Phase == Starting && processFound {
		if process.Phase == supervision.Running {
			current.Refusal, current.Error = "", ""
			current.Phase = Probing
			current.NextProbe = now
		} else if process.Phase == supervision.Exited || process.Phase == supervision.Stopping {
			current.Refusal = process.StartRefusal
			current.Error = process.StartError
			if current.Error == "" {
				current.Error = process.ExitError
			}
			if current.Error == "" {
				current.Error = "Codex exec-server exited during startup"
			}
			current.RestartFailures++
			current.Phase = Stopping
		}
		if err := write(); err != nil {
			return err
		}
	}
	if (current.Phase == Probing || current.Phase == Ready) && processFound && process.Phase != supervision.Running {
		current.Error = process.ExitError
		if current.Error == "" {
			current.Error = "Codex exec-server exited"
		}
		current.RestartFailures++
		current.Phase = Stopping
		current.RetryAt = time.Time{}
		if err := write(); err != nil {
			return err
		}
	}

	if probeDone {
		probeErr := probeResult.Err()
		if current.Phase == Probing && probeErr != nil && current.Error != probeErr.Error() {
			zap.L().Warn("Codex exec-server readiness probe failed", zap.String("process_id", current.ProcessID), zap.Error(probeErr))
		}
		switch current.Phase {
		case Probing:
			if probeErr == nil {
				current.Phase = Ready
				current.Error = ""
				current.Refusal = ""
				current.RestartFailures = 0
				current.ConsecutiveProbeFailures = 0
				current.NextProbe = now.Add(healthInterval)
				zap.L().Info("Codex exec-server ready", zap.String("url", h.config.URL))
			} else if !now.Before(current.ReadyDeadline) {
				current.Error = fmt.Sprintf("Codex exec-server did not become ready within %s: %v", h.config.Readiness.Timeout, probeErr)
				current.RestartFailures++
				current.Phase = Stopping
			} else {
				current.Error = probeErr.Error()
				current.NextProbe = now.Add(h.config.Readiness.ProbeInterval)
			}
		case Ready:
			if probeErr == nil {
				current.ConsecutiveProbeFailures = 0
				current.Error = ""
				current.RestartFailures = 0
				current.NextProbe = now.Add(healthInterval)
			} else {
				current.ConsecutiveProbeFailures++
				current.Error = probeErr.Error()
				zap.L().Warn("Codex exec-server health probe failed", zap.Int("consecutive_failures", int(current.ConsecutiveProbeFailures)), zap.Error(probeErr))
				if current.ConsecutiveProbeFailures >= 3 {
					current.Error = fmt.Sprintf("executor failed %d consecutive health probes: %v", current.ConsecutiveProbeFailures, probeErr)
					current.RestartFailures++
					current.Phase = Stopping
				} else {
					current.NextProbe = now.Add(healthInterval)
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
		if current.Phase != Stopped && current.Phase != Stopping {
			current.Phase = Stopping
			current.Error = ""
		}
		if current.Phase == Stopping {
			if err := c.reconcileStop(ctx, r, h, &current, write); err != nil {
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
	if desired.Mode != lifecycle.Running && current.Phase == Retrying && current.ProcessID == "" {
		current.Phase = Stopped
		current.RetryAt = time.Time{}
		return write()
	}

	if current.Phase == Stopping {
		if err := c.reconcileStop(ctx, r, h, &current, write); err != nil {
			return err
		}
		return write()
	}

	if current.Phase == Starting {
		return c.ensureProcessIntent(ctx, r, h, desired, current)
	}
	if current.Phase == Probing {
		if !now.Before(current.ReadyDeadline) {
			lastProbeError := current.Error
			current.Error = "Codex exec-server did not become ready before the readiness deadline"
			if lastProbeError != "" {
				current.Error += ": " + lastProbeError
			}
			current.RestartFailures++
			current.Phase = Stopping
			if err := write(); err != nil {
				return err
			}
			h.cancelProbe()
			return nil
		}
		if probeResult != nil {
			return controller.NewRequeueError(nil, min(h.config.Readiness.ProbeInterval, current.ReadyDeadline.Sub(now)))
		}
		if h.probePending() {
			return nil
		}
		if now.Before(current.NextProbe) {
			return controller.NewRequeueError(nil, min(current.NextProbe.Sub(now), current.ReadyDeadline.Sub(now)))
		}
		if err := write(); err != nil {
			return err
		}
		h.startProbe(func(ctx context.Context) error {
			probeCtx, cancel := context.WithDeadline(ctx, current.ReadyDeadline)
			defer cancel()
			probeCtx, cancel = context.WithTimeout(probeCtx, h.config.Readiness.ProbeTimeout)
			defer cancel()
			return h.probe(probeCtx)
		})
		return nil
	}

	if current.Phase == Ready {
		if h.probePending() {
			return nil
		}
		if now.Before(current.NextProbe) {
			return controller.NewRequeueError(nil, current.NextProbe.Sub(now))
		}
		h.startProbe(func(ctx context.Context) error {
			probeCtx, cancel := context.WithTimeout(ctx, healthDeadline)
			defer cancel()
			return h.probe(probeCtx)
		})
		return nil
	}

	if current.Phase == Retrying {
		if desired.Mode != lifecycle.Running || input.Demand == 0 {
			current.Phase = Stopped
			current.RetryAt = time.Time{}
			return write()
		}
		if now.Before(current.RetryAt) {
			return controller.NewRequeueError(nil, current.RetryAt.Sub(now))
		}
	}

	if current.Phase == Stopped || current.Phase == Retrying {
		if desired.Mode != lifecycle.Running || input.Demand == 0 {
			return nil
		}
		current.Phase = Starting
		current.Generation++
		current.ProcessID = fmt.Sprintf("%s-%d", ResourceID, current.Generation)
		current.ReadyDeadline = now.Add(h.config.Readiness.Timeout)
		current.NextProbe = time.Time{}
		current.RetryAt = time.Time{}
		current.Error = ""
		current.Refusal = ""
		if err := write(); err != nil {
			return err
		}
		return c.ensureProcessIntent(ctx, r, h, desired, current)
	}

	return write()
}

func (*LifecycleController) ensureProcessIntent(ctx context.Context, r controller.QRuntime, h *Host, desired lifecycle.Desired, current Runtime) error {
	return h.admission(ctx, desired, func(ctx context.Context) error {
		return h.processes.Request(ctx, r, current.ProcessID, supervision.DesiredState{
			Owner: ControllerName, Generation: current.Generation,
			StartDeadline: current.ReadyDeadline, StopTimeout: fenceDeadline,
		}, h.startProcess)
	})
}

func (*LifecycleController) reconcileStop(ctx context.Context, r controller.QRuntime, h *Host, current *Runtime, write func() error) error {
	h.cancelProbe()
	if h.probePending() {
		return write()
	}
	if current.ProcessID != "" {
		ptr := resource.NewMetadata(lifecycle.Namespace, supervision.DesiredStateType, current.ProcessID, resource.VersionUndefined)
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
	desired, _, err := lifecycle.ReadDesired(ctx, r)
	if err != nil {
		return err
	}
	if desired.Mode != lifecycle.Running {
		current.Phase = Stopped
		current.Error, current.Refusal = "", ""
		current.RetryAt = time.Time{}
	} else {
		current.Phase = Retrying
		current.RetryAt = time.Now().Add(restartDelay(current.RestartFailures))
	}
	if err := write(); err != nil {
		return err
	}
	if current.Phase == Retrying {
		return controller.NewRequeueError(nil, time.Until(current.RetryAt))
	}
	return nil
}

func restartDelay(failures uint32) time.Duration {
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

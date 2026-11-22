package coordinator

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/backend"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/service"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"go.uber.org/zap"
)

const (
	executionFenceGrace            = 150 * time.Second
	executionServiceSetupAllowance = 90*time.Second + 30*time.Second
)

// ErrFencedStartup marks an interrupted embedded start whose service lifetime
// has already been joined and fenced. A later caller may request it again
// without adopting work from the canceled startup.
// That retry is unproven for a replacement incarnation's canceled start: https://github.com/agentydragon/ducktape/issues/16412
var ErrFencedStartup = errors.New("embedded Execution startup was fenced")

// executionServiceHandle owns one in-process Go Execution API lifetime. The
// typed adapter is published only after backend readiness; backend cleanup
// must finish before this handle can be replaced.
type executionServiceHandle struct {
	executor     executorpb.ExecutorServer
	fence        func(context.Context) error
	mu           sync.Mutex
	fenceMu      sync.Mutex
	fenced       bool
	expectedStop bool
	exited       chan childExit
	cancel       context.CancelFunc
	done         chan struct{}
	result       childExit
	ready        chan executionReadyState
	backendLife  context.Context
}

type executionReadyState struct {
	executor executorpb.ExecutorServer
	lifetime context.Context
}

func newExecutionServiceHandle(config service.Config, run func(context.Context, service.Config, func(executorpb.ExecutorServer, context.Context)) error) *executionServiceHandle {
	lifetime, cancel := context.WithCancel(context.Background())
	h := &executionServiceHandle{cancel: cancel, done: make(chan struct{}), exited: make(chan childExit, 1), ready: make(chan executionReadyState, 1)}
	go func() {
		err := run(lifetime, config, func(executor executorpb.ExecutorServer, backendLife context.Context) {
			h.ready <- executionReadyState{executor: executor, lifetime: backendLife}
		})
		h.mu.Lock()
		switch {
		case errors.Is(err, service.ErrFencedBackendLoss):
			// service.RunEmbedded returns this only after adapter shutdown and native
			// process-domain fencing have completed.
			zap.L().Warn(
				"embedded Execution backend loss fenced",
				zap.String("sandbox_namespace", config.Namespace),
				zap.String("sandbox_name", config.SandboxName),
				zap.String("sandbox_uid", config.SandboxUID),
				zap.String("incarnation_uid", config.IncarnationUID),
				zap.String("backend", string(config.Owner)),
				zap.String("loss_reason", err.Error()),
			)
		case err == nil && h.expectedStop:
		case err == nil:
			h.result = childExit{code: 1, err: errors.New("embedded Execution service exited without a stop request or backend fence")}
		default:
			h.result = childExit{code: 1, err: err}
		}
		close(h.done)
		h.exited <- h.result
		close(h.exited)
		h.mu.Unlock()
	}()
	return h
}

func (h *executionServiceHandle) Executor() executorpb.ExecutorServer { return h.executor }

func (h *executionServiceHandle) StopAndFence(ctx context.Context) error {
	h.fenceMu.Lock()
	defer h.fenceMu.Unlock()
	if h.fenced {
		return nil
	}

	h.mu.Lock()
	h.expectedStop = true
	h.mu.Unlock()
	var err error
	if h.fence != nil {
		err = h.fence(ctx)
	} else if h.cancel == nil || h.done == nil {
		err = errors.New("execution service has no active lifetime")
	} else {
		h.cancel()
		select {
		case <-h.done:
			h.mu.Lock()
			result := h.result
			h.mu.Unlock()
			if result.code != 0 || result.err != nil {
				err = errors.Join(result.err, errors.New("embedded Execution service exited without backend fence proof"))
			}
		case <-ctx.Done():
			err = ctx.Err()
		}
	}
	if err == nil {
		h.fenced = true
	}
	return err
}

func (h *executionServiceHandle) ProcessExit() <-chan childExit { return h.exited }

func (h *executionServiceHandle) stopRequested() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.expectedStop
}

func (h *executionServiceHandle) serving() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.expectedStop {
		return false
	}
	if h.backendLife != nil && h.backendLife.Err() != nil {
		return false
	}
	select {
	case <-h.done:
		return false
	default:
		return true
	}
}

func (r *Runtime) startExecutionService(ctx context.Context, config Config, incarnationUID string) (executionHandle, error) {
	serviceConfig, err := runtimeExecutionServiceConfig(config, incarnationUID, r.processes, r.executionSupervisor, r.commandSupervisor)
	if err != nil {
		return nil, err
	}
	r.mu.Lock()
	serviceConfig.ProcessGeneration = r.desired.Generation
	r.mu.Unlock()
	handle := newExecutionServiceHandle(serviceConfig, service.RunEmbedded)
	stopUnready := func(cause error) (executionHandle, error) {
		fenceCtx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
		defer cancel()
		if err := handle.StopAndFence(fenceCtx); err != nil {
			return handle, errors.Join(cause, err)
		}
		return nil, errors.Join(ErrFencedStartup, cause)
	}
	readyCtx, cancel := context.WithTimeout(ctx, executionServiceReadyTimeout(serviceConfig.Readiness.Timeout))
	defer cancel()
	select {
	case ready := <-handle.ready:
		handle.executor = ready.executor
		handle.backendLife = ready.lifetime
	case <-handle.done:
		return stopUnready(errors.Join(errors.New("Execution API exited before readiness"), handle.result.err))
	case <-readyCtx.Done():
		return stopUnready(fmt.Errorf("wait for Execution API readiness: %w", readyCtx.Err()))
	}
	select {
	case <-handle.done:
		return stopUnready(errors.Join(errors.New("Execution API exited at readiness"), handle.result.err))
	default:
	}
	return handle, nil
}

func runtimeExecutionServiceConfig(config Config, incarnationUID string, processes *supervision.Host, supervisor *supervision.Supervisor, commandSupervisor *supervision.Supervisor) (service.Config, error) {
	if config.ExecutionBackend == nil {
		return service.Config{}, errors.New("Execution service requires a configured execution backend")
	}
	serviceConfig := service.Config{
		ProcessHost: processes, Supervisor: supervisor, CommandSupervisor: commandSupervisor,
		Owner: config.ExecutionBackend.Owner, Placement: config.ExecutionBackend.Placement,
		Namespace: config.Launch.Sandbox.Namespace, SandboxName: config.Launch.Sandbox.Name,
		SandboxUID: config.Launch.Sandbox.UID, IncarnationUID: incarnationUID,
		StateDir:    filepath.Join(config.StateDir, "execution"),
		ExecutorURL: config.ExecutionBackend.URL, LimitsJSON: config.ExecutionBackend.LimitsJSON, Environment: config.HarnessEnvironment,
		Implementation: config.ExecutionBackend.Implementation, Readiness: config.ExecutionBackend.Readiness,
	}
	switch serviceConfig.Owner {
	case backend.OwnerSystemd:
		launcher := config.ExecutionBackend.Launcher
		if launcher == nil {
			return service.Config{}, errors.New("systemd executor owner requires its explicit launcher")
		}
		serviceConfig.Binary = "/run/agentplane/environment/bin/agentplane-execution-service"
		serviceConfig.WorkingDirectory = config.StateDir
		serviceConfig.CodexBinary = "/run/current-system/sw/bin/codex"
		if config.Codex != nil {
			serviceConfig.CodexBinary = config.Codex.Binary
		}
	case backend.OwnerCompanion:
		if config.ExecutionBackend.ContainerSocket == "" {
			return service.Config{}, errors.New("companion executor owner requires its ownership socket")
		}
		serviceConfig.ContainerSocket = config.ExecutionBackend.ContainerSocket
	}
	serviceConfig, err := serviceConfig.Resolve()
	if err != nil {
		return service.Config{}, fmt.Errorf("execution service configuration: %w", err)
	}
	return serviceConfig, nil
}

// executionServiceReadyTimeout covers setup before the configured executor probe
// window. Its 120s setup allowance covers
// the companion's process-domain fence (up to 90s) plus owner preparation, claim or
// native startup. This is one outer allowance rather than separate phase deadlines;
// an expired deadline still requires StopAndFence proof before return.
func executionServiceReadyTimeout(probeWindow time.Duration) time.Duration {
	return addExecutionDurationsSaturating(probeWindow, executionServiceSetupAllowance)
}

func addExecutionDurationsSaturating(values ...time.Duration) time.Duration {
	const maxDuration = time.Duration(1<<63 - 1)
	total := time.Duration(0)
	for _, value := range values {
		if value > 0 && total > maxDuration-value {
			return maxDuration
		}
		total += value
	}
	return total
}

package runtime

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/service"
)

const (
	executionFenceGrace = 150 * time.Second
	executionReadyWait  = 30 * time.Second
)

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
	ready        chan executorpb.ExecutorServer
}

func newExecutionServiceHandle(config service.Config, run func(context.Context, service.Config, func(executorpb.ExecutorServer)) error) *executionServiceHandle {
	lifetime, cancel := context.WithCancel(context.Background())
	h := &executionServiceHandle{cancel: cancel, done: make(chan struct{}), exited: make(chan childExit, 1), ready: make(chan executorpb.ExecutorServer, 1)}
	go func() {
		err := run(lifetime, config, func(executor executorpb.ExecutorServer) { h.ready <- executor })
		h.mu.Lock()
		switch {
		case errors.Is(err, service.ErrFencedBackendLoss):
			// service.RunEmbedded returns this only after adapter shutdown and native
			// process-domain fencing have completed.
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

func (r *Runtime) startExecutionService(ctx context.Context, config Config, incarnationUID string) (executionHandle, error) {
	serviceConfig := service.Config{
		Namespace: config.Launch.Sandbox.Namespace, SandboxName: config.Launch.Sandbox.Name,
		SandboxUID: config.Launch.Sandbox.UID, IncarnationUID: incarnationUID,
		StateDir: filepath.Join(config.StateDir, "execution"), SocketPath: config.ExecutionSocket,
		ExecutorURL: config.CodexExecServer.URL, Environment: config.HarnessEnvironment,
	}
	if config.Supervision == "systemd" {
		launcher := config.CodexExecServer.Launcher
		if launcher == nil {
			return nil, errors.New("VM execution service requires its explicit systemd launcher")
		}
		serviceConfig.Backend = "systemd"
		serviceConfig.Binary = config.CodexBinary
		serviceConfig.WorkingDirectory = config.StateDir
		serviceConfig.Systemd = &service.SystemdConfig{
			AgentUID: launcher.AgentUID, AgentGID: launcher.AgentGID, BlockDevices: launcher.BlockDevices,
			AggregateLimits: launcher.AggregateLimits, ProcessLimits: launcher.ProcessLimits,
			OwnerUnit: RuntimeUnitName, LifetimeDirectory: "/run/agentplane-environment-runtime-lifetime",
		}
	} else {
		if config.CodexExecServer.ContainerSocket == "" {
			return nil, errors.New("Pod execution service requires the companion ownership socket")
		}
		serviceConfig.Backend = "companion"
		serviceConfig.ContainerSocket = config.CodexExecServer.ContainerSocket
	}
	if err := serviceConfig.Validate(); err != nil {
		return nil, fmt.Errorf("execution service configuration: %w", err)
	}
	handle := newExecutionServiceHandle(serviceConfig, service.RunEmbedded)
	stopUnready := func(cause error) (executionHandle, error) {
		fenceCtx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
		defer cancel()
		return nil, errors.Join(cause, handle.StopAndFence(fenceCtx))
	}
	readyCtx, cancel := context.WithTimeout(ctx, executionReadyWait)
	defer cancel()
	select {
	case executor := <-handle.ready:
		handle.executor = executor
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

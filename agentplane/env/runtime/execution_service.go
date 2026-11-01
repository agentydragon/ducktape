package runtime

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/service"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

const (
	// Service shutdown drains Executor calls and may use 90 seconds to prove the
	// companion's fresh idle generation before returning cleanly.
	executionStopGrace      = 110 * time.Second
	executionFenceGrace     = 150 * time.Second
	executionReadyWait      = 30 * time.Second
	executionHealthInterval = 5 * time.Second
	executionHealthFailures = 3
	executionHealthDeadline = 3 * time.Second
	vmExecutionConfig       = "/run/agentplane/execution-service/config.json"
)

type supervisedExecutionHandle struct {
	conn         *grpc.ClientConn
	process      childProcess
	fence        func(context.Context) error
	mu           sync.Mutex
	fenceMu      sync.Mutex
	fenced       bool
	expectedStop bool
	healthCancel context.CancelFunc
	exited       chan childExit
}

func (h *supervisedExecutionHandle) ClientConn() *grpc.ClientConn { return h.conn }

func (h *supervisedExecutionHandle) StopAndFence(ctx context.Context) error {
	h.fenceMu.Lock()
	defer h.fenceMu.Unlock()
	if h.fenced {
		return nil
	}

	h.mu.Lock()
	h.expectedStop = true
	stopHealth := h.healthCancel
	h.mu.Unlock()
	if stopHealth != nil {
		stopHealth()
	}
	var err error
	if h.fence != nil {
		err = h.fence(ctx)
	} else if h.process == nil {
		err = errors.New("execution service has no supervised process")
	} else {
		stopErr := h.process.Stop(ctx)
		code, waitErr := h.process.Wait(ctx)
		if code != 0 {
			description := ""
			if process, ok := h.process.(interface{ ExitDescription() string }); ok {
				description = process.ExitDescription()
			}
			waitErr = errors.Join(waitErr, fmt.Errorf("execution service exited with status %d; no backend fence proof: %s", code, description))
		}
		err = errors.Join(stopErr, waitErr)
	}
	if err == nil {
		h.fenced = true
	}
	return err
}

func (h *supervisedExecutionHandle) ProcessExit() <-chan childExit { return h.exited }

func (h *supervisedExecutionHandle) stopRequested() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.expectedStop
}

func (h *supervisedExecutionHandle) Close() error {
	h.mu.Lock()
	stopHealth := h.healthCancel
	h.healthCancel = nil
	if h.conn == nil {
		h.mu.Unlock()
		return nil
	}
	conn := h.conn
	h.conn = nil
	h.mu.Unlock()
	if stopHealth != nil {
		stopHealth()
	}
	return conn.Close()
}

func (h *supervisedExecutionHandle) monitorHealth(ctx context.Context, interval, deadline time.Duration, failureLimit int) {
	err := monitorExecutionHealth(ctx, h.conn, interval, deadline, failureLimit)
	if err == nil {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.expectedStop || h.exited == nil {
		return
	}
	h.exited <- childExit{healthFailure: true, err: err}
}

func monitorExecutionHealth(ctx context.Context, conn *grpc.ClientConn, interval, deadline time.Duration, failureLimit int) error {
	if interval <= 0 || deadline <= 0 || failureLimit <= 0 {
		return errors.New("Execution health monitor requires positive intervals and failure limit")
	}
	client := healthpb.NewHealthClient(conn)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	failures := 0
	var lastErr error
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
		checkCtx, cancel := context.WithTimeout(ctx, deadline)
		response, err := client.Check(checkCtx, &healthpb.HealthCheckRequest{Service: executorpb.Executor_ServiceDesc.ServiceName})
		cancel()
		if err == nil && response.Status == healthpb.HealthCheckResponse_SERVING {
			failures = 0
			lastErr = nil
			continue
		}
		if err == nil {
			err = fmt.Errorf("Execution service health is %s", response.Status)
		}
		lastErr = err
		failures++
		if failures >= failureLimit {
			return fmt.Errorf("Execution service health failed %d consecutive checks: %w", failures, lastErr)
		}
	}
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
	configPath := filepath.Join(config.StateDir, "runtime", "execution.json")
	if config.Supervision == "systemd" {
		configPath = vmExecutionConfig
	}
	if err := writeExecutionConfig(configPath, serviceConfig); err != nil {
		return nil, err
	}
	var handle *supervisedExecutionHandle
	if config.Supervision == "systemd" {
		invocation, err := r.deps.startUnit(ctx, ExecutionUnitName)
		if invocation.reference != nil || invocation.ID != "" {
			r.mu.Lock()
			r.executionUnit = invocation
			r.mu.Unlock()
		}
		if err != nil {
			return nil, err
		}
		handle = &supervisedExecutionHandle{exited: make(chan childExit, 1), fence: func(fenceCtx context.Context) error {
			if err := r.deps.fenceUnit(fenceCtx, ExecutionUnitName, invocation); err != nil {
				return err
			}
			r.clearExecutionUnit(invocation)
			return nil
		}}
	} else {
		binary := config.ExecutionBinary
		if binary == "" {
			current, err := os.Executable()
			if err != nil {
				return nil, fmt.Errorf("resolve execution service sibling binary: %w", err)
			}
			binary = filepath.Join(filepath.Dir(current), "agentplane-execution-service")
		}
		process, err := r.deps.startChild(r.supervisor, supervision.ProcessConfig{
			Command: []string{binary, "--config", configPath}, Env: inheritedEnvironment(), StopGrace: executionStopGrace,
		})
		if err != nil {
			return nil, fmt.Errorf("start standalone execution service: %w", err)
		}
		if err := process.Start(ctx); err != nil {
			return nil, errors.Join(err, process.Stop(context.Background()))
		}
		// Drain child output into Runtime's log stream. Besides avoiding blocked
		// pipes, this preserves the Execution service's native fence diagnostics.
		for _, output := range []io.Reader{process.Stdout(), process.Stderr()} {
			if output == nil {
				continue
			}
			r.processOutputWait.Add(1)
			go func(reader io.Reader) {
				defer r.processOutputWait.Done()
				_, _ = io.Copy(os.Stderr, reader)
			}(output)
		}
		handle = &supervisedExecutionHandle{process: process, exited: make(chan childExit, 1)}
		go func() {
			code, err := process.Wait(context.Background())
			handle.exited <- childExit{code: code, err: err}
			close(handle.exited)
		}()
	}
	conn, err := dialUnix(config.ExecutionSocket)
	if err != nil {
		return nil, errors.Join(err, handle.StopAndFence(context.Background()), handle.Close())
	}
	handle.conn = conn
	readyCtx, cancel := context.WithTimeout(ctx, executionReadyWait)
	defer cancel()
	if err := waitExecutionReady(readyCtx, conn); err != nil {
		return nil, errors.Join(fmt.Errorf("wait for Execution service readiness: %w", err), handle.StopAndFence(context.Background()), handle.Close())
	}
	if config.Supervision == "systemd" {
		healthCtx, stopHealth := context.WithCancel(context.Background())
		handle.healthCancel = stopHealth
		go handle.monitorHealth(healthCtx, executionHealthInterval, executionHealthDeadline, executionHealthFailures)
	}
	return handle, nil
}

func writeExecutionConfig(path string, config service.Config) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	if err := os.Chmod(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.Marshal(config)
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".execution-*.json")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return err
	}
	if _, err := temporary.Write(data); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}
	directory, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func waitExecutionReady(ctx context.Context, conn *grpc.ClientConn) error {
	client := healthpb.NewHealthClient(conn)
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		checkCtx, cancel := context.WithTimeout(ctx, time.Second)
		response, err := client.Check(checkCtx, &healthpb.HealthCheckRequest{Service: executorpb.Executor_ServiceDesc.ServiceName})
		cancel()
		if err == nil && response.Status == healthpb.HealthCheckResponse_SERVING {
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

func inheritedEnvironment() map[string]string {
	result := map[string]string{}
	for _, entry := range os.Environ() {
		key, value, ok := strings.Cut(entry, "=")
		if ok {
			result[key] = value
		}
	}
	return result
}

package service

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"time"

	execution "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/adapter"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/sethvargo/go-retry"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

// ErrFencedBackendLoss reports an established executor backend loss after the
// Execution adapter drained and the backend's process domain was fenced. For the
// companion backend this includes proof of a fresh idle executor generation.
// The command maps this specific outcome to a clean process exit so Runtime can
// replace the API process without replacing the Harness Host.
var ErrFencedBackendLoss = errors.New("execution backend loss was fenced")

// Run owns the backend from startup fencing until shutdown. The caller must be
// PID 1 for the direct pod backend: returning lets the container runtime fence escaped descendants.
// Systemd callers supply an owner unit and lifetime directory; death of that
// owner fences its bound transient native units.
func Run(ctx context.Context, config Config) (result error) {
	return runOwned(ctx, config, nil)
}

// RunEmbedded keeps the native backend and private health socket under the
// Runtime process lifetime. The ready callback publishes the typed adapter to
// Runtime after native readiness; the Executor RPC is not served on this socket.
func RunEmbedded(ctx context.Context, config Config, ready func(executorpb.ExecutorServer)) error {
	if ready == nil {
		return errors.New("embedded Execution API requires a ready callback")
	}
	return runOwned(ctx, config, ready)
}

func runOwned(ctx context.Context, config Config, ready func(executorpb.ExecutorServer)) (result error) {
	if err := config.Validate(); err != nil {
		return err
	}
	owner, err := lock(config.StateDir)
	if err != nil {
		return err
	}
	defer owner.Close()
	var supervisor *supervision.Supervisor
	switch config.Backend {
	case "pod":
		supervisor, err = supervision.NewPID1Supervisor(owner)
	case "companion":
		fence, finish := context.WithTimeout(ctx, 90*time.Second)
		err = codexexecserver.FenceContainer(fence, config.ContainerSocket, "")
		finish()
	case "systemd":
		ownerUnit, lifetimeDirectory := config.Systemd.OwnerUnit, config.Systemd.LifetimeDirectory
		launcher := &supervision.SystemdLauncher{
			Slice:    supervision.ExecutorSlice,
			AgentUID: config.Systemd.AgentUID, AgentGID: config.Systemd.AgentGID,
			BlockDevices:    config.Systemd.BlockDevices,
			AggregateLimits: config.Systemd.AggregateLimits, ProcessLimits: config.Systemd.ProcessLimits,
		}
		supervisor, err = launcher.PrepareForOwner(config.StateDir, config.SandboxUID, supervision.SystemdOwner{
			ScopeID: supervision.SystemdScopeID("execution-service"),
			Unit:    ownerUnit, LifetimeDirectory: lifetimeDirectory,
		})
	}
	if err != nil {
		return err
	}
	return runWithReady(ctx, config, codexexecserver.NativeStart(codexexecserver.NativeConfig{
		Config: codexexecserver.Config{URL: config.ExecutorURL, ContainerSocket: config.ContainerSocket}, Binary: config.Binary,
		StateDir: config.WorkingDirectory, Environment: config.Environment, Supervisor: supervisor,
	}), ready)
}

func lock(directory string) (*os.File, error) {
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	// Kubernetes may add fsGroup permissions to a persistent directory when a
	// volume is mounted into a replacement Pod. Open the directory without
	// following a replacement symlink, then restore its private mode through the
	// file descriptor before creating or opening the owner lock.
	directoryFD, err := syscall.Open(directory, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	directoryFile := os.NewFile(uintptr(directoryFD), directory)
	defer directoryFile.Close()
	if err := syscall.Fchmod(directoryFD, 0700); err != nil {
		return nil, fmt.Errorf("make execution state directory private: %w", err)
	}
	var info syscall.Stat_t
	if err := syscall.Fstat(directoryFD, &info); err != nil {
		return nil, err
	}
	// In standalone mode the socket admits trusted local environment components
	// with full execution authority. Its mode is part of that authorization.
	if info.Mode&syscall.S_IFMT != syscall.S_IFDIR || info.Mode&0077 != 0 {
		return nil, errors.New("execution state directory must be private (0700)")
	}
	ownerFD, err := syscall.Openat(directoryFD, ".agentplane-execution-owner", syscall.O_RDWR|syscall.O_CREAT|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, err
	}
	owner := os.NewFile(uintptr(ownerFD), filepath.Join(directory, ".agentplane-execution-owner"))
	if err := syscall.Flock(ownerFD, syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = owner.Close()
		return nil, fmt.Errorf("execution service already owns state directory: %w", err)
	}
	return owner, nil
}

func run(ctx context.Context, config Config, start codexexecserver.Start) (result error) {
	return runWithReady(ctx, config, start, nil)
}

func runWithReady(ctx context.Context, config Config, start codexexecserver.Start, ready func(executorpb.ExecutorServer)) (result error) {
	return runWithServer(ctx, config, start, ready, func(server *grpc.Server, listener net.Listener) error {
		return server.Serve(listener)
	})
}

func runWithServer(ctx context.Context, config Config, start codexexecserver.Start, ready func(executorpb.ExecutorServer), serve func(*grpc.Server, net.Listener) error) (result error) {
	lifetime, cancel := context.WithCancel(ctx)
	defer cancel()
	instance, err := start(lifetime)
	if err != nil {
		return err
	}
	var fencedBackendLoss error
	defer func() {
		stopCtx, stop := context.WithTimeout(context.Background(), 90*time.Second)
		defer stop()
		stopErr := instance.Stop(stopCtx)
		result = errors.Join(result, stopErr)
		if fencedBackendLoss != nil && result == nil {
			result = errors.Join(ErrFencedBackendLoss, fencedBackendLoss)
		}
	}()
	backendDone := make(chan error, 1)
	go func() {
		select {
		case err := <-instance.Done:
			backendDone <- err
			cancel()
		case <-lifetime.Done():
		}
	}()
	readiness, endReadiness := context.WithTimeout(lifetime, 20*time.Second)
	err = retry.Do(readiness, retry.NewConstant(100*time.Millisecond), func(ctx context.Context) error {
		probeCtx, endProbe := context.WithTimeout(ctx, 2*time.Second)
		defer endProbe()
		if err := codexexecserver.ProbeAt(config.ExecutorURL)(probeCtx); err != nil {
			return retry.RetryableError(err)
		}
		return nil
	})
	endReadiness()
	if err != nil {
		return fmt.Errorf("execution backend readiness: %w", err)
	}
	go func() {
		if err := codexexecserver.Monitor(lifetime, config.ExecutorURL); err != nil {
			select {
			case backendDone <- err:
			default:
			}
			cancel()
		}
	}()
	adapter, err := execution.New(execution.Config{
		Context: lifetime, Admit: config.admit,
		Acquire: func(context.Context) (string, error) { return config.ExecutorURL, nil },
	})
	if err != nil {
		return err
	}
	defer adapter.Close()
	// Only the state-directory lock holder may replace the previous socket.
	if err := os.Remove(config.Socket()); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	listener, err := net.Listen("unix", config.Socket())
	if err != nil {
		return err
	}
	defer listener.Close()
	if err := os.Chmod(config.Socket(), 0600); err != nil {
		return err
	}
	server := grpc.NewServer()
	if ready == nil {
		executorpb.RegisterExecutorServer(server, adapter)
	}
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	if ready == nil {
		healthServer.SetServingStatus(executorpb.Executor_ServiceDesc.ServiceName, healthpb.HealthCheckResponse_SERVING)
	}
	healthpb.RegisterHealthServer(server, healthServer)
	served := make(chan error, 1)
	go func() { served <- serve(server, listener) }()
	if ready != nil {
		err = waitEmbeddedHealth(lifetime, config.Socket(), served)
		if err == nil {
			ready(adapter)
		} else if errors.Is(err, context.Canceled) {
			// Backend loss cancels lifetime after publishing its exact loss
			// evidence. Preserve that evidence even when the health probe was
			// still waiting; otherwise a fenced loss becomes an unproven exit.
			select {
			case backendErr := <-backendDone:
				if backendErr == nil {
					err = errors.New("execution backend stopped")
				} else {
					err = backendErr
				}
			default:
				// A Runtime-requested stop cancels the parent context. The
				// native Stop below still has to prove the backend fence, but
				// the interrupted health probe is not itself a service failure.
				// An independent readiness deadline leaves ctx live and keeps
				// its error.
				if ctx.Err() != nil {
					err = nil
				}
			}
		}
	}
	if err == nil {
		select {
		case err = <-served:
		case <-lifetime.Done():
			select {
			case err = <-backendDone:
				if err == nil {
					err = errors.New("execution backend stopped")
				}
			default:
			}
		}
	}
	if errors.Is(err, codexexecserver.ErrExecutionBackendLost) {
		fencedBackendLoss = err
		err = nil
	}
	// Cancel sessions before draining RPCs: OpenSession streams are held until
	// explicitly canceled, and a canceled unary call alone never stops native work.
	cancel()
	healthServer.Shutdown()
	if closeErr := adapter.Close(); closeErr != nil {
		err = errors.Join(err, closeErr)
	}
	shutdown, finish := context.WithTimeout(context.Background(), 5*time.Second)
	defer finish()
	graceful.Stop(shutdown, server)
	return err
}

func waitEmbeddedHealth(ctx context.Context, socket string, served <-chan error) error {
	conn, err := grpc.NewClient("unix://"+socket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return fmt.Errorf("connect to embedded Execution health socket: %w", err)
	}
	defer conn.Close()
	client := healthpb.NewHealthClient(conn)
	readiness, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	err = retry.Do(readiness, retry.NewConstant(20*time.Millisecond), func(ctx context.Context) error {
		select {
		case serveErr := <-served:
			if serveErr == nil {
				serveErr = errors.New("health server stopped")
			}
			return fmt.Errorf("embedded Execution health server exited before readiness: %w", serveErr)
		default:
		}
		checkCtx, finish := context.WithTimeout(ctx, 100*time.Millisecond)
		defer finish()
		response, err := client.Check(checkCtx, &healthpb.HealthCheckRequest{})
		if err != nil {
			return retry.RetryableError(err)
		}
		if response.GetStatus() != healthpb.HealthCheckResponse_SERVING {
			return retry.RetryableError(errors.New("embedded Execution health is not serving"))
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("embedded Execution health readiness: %w", err)
	}
	select {
	case serveErr := <-served:
		if serveErr == nil {
			serveErr = errors.New("health server stopped")
		}
		return fmt.Errorf("embedded Execution health server exited at readiness: %w", serveErr)
	default:
	}
	return nil
}

// Filesystem access to the standalone Executor socket authenticates the environment owner.
// Embedded mode serves only health there. Remote caller authorization and routing remain with Gateway. This service
// checks one immutable incarnation; it cannot adopt or replace a Runner epoch.
func (c Config) admit(ctx context.Context, scope *executorpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
	if scope == nil || scope.Destination == nil || scope.Destination.Owner == nil || scope.SessionId == "" || len(scope.SessionId) > 128 {
		return status.Error(codes.InvalidArgument, "execution scope is incomplete")
	}
	destination := scope.Destination
	if destination.Sandbox != c.SandboxName || destination.SandboxUid != c.SandboxUID ||
		destination.Owner.Namespace != c.Namespace || destination.Owner.Name != c.SandboxName {
		return status.Error(codes.PermissionDenied, "execution destination names another Sandbox")
	}
	if scope.ExpectedIncarnationUid != c.IncarnationUID {
		return status.Error(codes.FailedPrecondition, "Sandbox incarnation changed")
	}
	if err := ctx.Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	return accept(ctx)
}

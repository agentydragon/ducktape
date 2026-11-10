package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	execution "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/adapter"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/sethvargo/go-retry"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// ErrFencedBackendLoss reports an established executor backend loss after the
// Execution adapter drained and the backend's process domain was fenced. For the
// companion backend this includes proof of a fresh idle executor generation.
// Runtime can replace the API lifetime without replacing Runtime.
var ErrFencedBackendLoss = errors.New("execution backend loss was fenced")

// RunEmbedded keeps the native backend under the Runtime process lifetime. The
// Runtime holds the environment owner and serializes attempts through its Execution
// controller; each prior service must return fenced before its successor starts.
// The ready callback publishes the typed adapter after native readiness.
func RunEmbedded(ctx context.Context, config Config, ready func(executorpb.ExecutorServer, context.Context)) error {
	if ready == nil {
		return errors.New("embedded Execution API requires a ready callback")
	}
	return runOwned(ctx, config, ready)
}

func runOwned(ctx context.Context, config Config, ready func(executorpb.ExecutorServer, context.Context)) (result error) {
	var err error
	config, err = config.Resolve()
	if err != nil {
		return err
	}
	var supervisor *supervision.Supervisor
	switch config.Backend {
	case "companion":
		fence, finish := context.WithTimeout(ctx, 90*time.Second)
		err = codexexecserver.FenceContainer(fence, config.ContainerSocket, "")
		finish()
	case "systemd":
		supervisor = config.Supervisor
	}
	if err != nil {
		return err
	}
	var launchProcess func(context.Context, supervision.ProcessConfig) (*supervision.ManagedProcess, error)
	if config.ProcessHost != nil && supervisor != nil {
		launchProcess = func(ctx context.Context, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return config.ProcessHost.Launch(ctx, "execution-service", config.ProcessGeneration, supervisor, processConfig)
		}
	}
	return runWithReady(ctx, config, codexexecserver.NativeStart(codexexecserver.NativeConfig{
		Config: codexexecserver.Config{URL: config.ExecutorURL, ContainerSocket: config.ContainerSocket}, Binary: config.Binary,
		StateDir: config.WorkingDirectory, Environment: config.Environment, Supervisor: supervisor, LaunchProcess: launchProcess,
	}), ready)
}

func runWithReady(ctx context.Context, config Config, start codexexecserver.Start, ready func(executorpb.ExecutorServer, context.Context)) (result error) {
	readinessConfig, err := config.Readiness.Resolve()
	if err != nil {
		return err
	}
	config.Readiness = readinessConfig
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
	readiness, endReadiness := context.WithTimeout(lifetime, config.Readiness.Timeout)
	err = retry.Do(readiness, retry.NewConstant(config.Readiness.ProbeInterval), func(ctx context.Context) error {
		probeCtx, endProbe := context.WithTimeout(ctx, config.Readiness.ProbeTimeout)
		defer endProbe()
		if err := codexexecserver.ProbeAt(config.ExecutorURL)(probeCtx); err != nil {
			return retry.RetryableError(err)
		}
		return nil
	})
	endReadiness()
	if err != nil {
		if errors.Is(err, context.Canceled) {
			select {
			case backendErr := <-backendDone:
				if backendErr == nil {
					backendErr = errors.New("execution backend stopped")
				}
				if errors.Is(backendErr, codexexecserver.ErrExecutionBackendLost) {
					fencedBackendLoss = backendErr
					return nil
				}
				return fmt.Errorf("execution backend readiness: %w", backendErr)
			default:
				if ctx.Err() != nil {
					// Runtime-requested shutdown can interrupt startup probing. The
					// deferred backend stop still has to establish its fence first.
					return nil
				}
			}
		}
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
	// A lost backend must not be published as ready. Its stop still has to
	// prove the process-domain fence before this service returns.
	if lifetime.Err() != nil {
		select {
		case backendErr := <-backendDone:
			if backendErr == nil {
				err = errors.New("execution backend stopped")
			} else {
				err = backendErr
			}
		default:
			if ctx.Err() == nil {
				err = lifetime.Err()
			}
		}
	} else {
		ready(adapter, lifetime)
	}
	if err == nil {
		<-lifetime.Done()

		select {
		case err = <-backendDone:
			if err == nil {
				err = errors.New("execution backend stopped")
			}
		default:
		}
	}
	if errors.Is(err, codexexecserver.ErrExecutionBackendLost) {
		fencedBackendLoss = err
		err = nil
	}
	// Cancel sessions before draining RPCs: OpenSession streams are held until
	// explicitly canceled, and a canceled unary call alone never stops native work.
	cancel()
	if closeErr := adapter.Close(); closeErr != nil {
		err = errors.Join(err, closeErr)
	}
	return err
}

// Runtime authenticates the environment owner; Gateway authorizes remote callers. This service
// checks one immutable incarnation; it cannot adopt or replace a Runner epoch.
func (c Config) admit(ctx context.Context, scope *executorpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
	if scope == nil || scope.Destination == nil || scope.Destination.Owner == nil || scope.SessionId == "" || len(scope.SessionId) > 128 {
		return status.Error(codes.InvalidArgument, "execution scope is incomplete")
	}
	destination := scope.Destination
	if destination.Target == nil || destination.Target.Namespace == "" || destination.Target.Name == "" || destination.Target.Uid == "" ||
		destination.Target.Namespace != c.Namespace || destination.Target.Name != c.SandboxName || destination.Target.Uid != c.SandboxUID ||
		destination.Owner == nil || destination.Owner.Namespace != destination.Target.Namespace || destination.Owner.Name != c.SandboxName {
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

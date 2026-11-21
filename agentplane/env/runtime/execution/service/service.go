package service

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/adapter"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/typedproxy"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/sethvargo/go-retry"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

// ErrFencedBackendLoss reports an established executor backend loss after the
// Execution adapter drained and the backend's process domain was fenced. For the
// companion backend this includes proof of a fresh idle executor generation.
// Runtime can replace the API lifetime without replacing Runtime.
var ErrFencedBackendLoss = errors.New("execution backend loss was fenced")

// RunEmbedded keeps the supervised backend under the Runtime process lifetime. The
// Runtime holds the environment owner and serializes attempts through its Execution
// controller; each prior service must return fenced before its successor starts.
// The ready callback publishes the typed adapter after backend readiness.
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
	switch config.Owner {
	case codexexecserver.OwnerCompanion:
		fence, finish := context.WithTimeout(ctx, 90*time.Second)
		err = codexexecserver.FenceContainer(fence, config.ContainerSocket, "")
		finish()
	case codexexecserver.OwnerSystemd:
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
		Config: codexexecserver.Config{
			URL: config.ExecutorURL, Owner: config.Owner, ContainerSocket: config.ContainerSocket,
			Implementation: config.Implementation, LimitsJSON: config.LimitsJSON, Placement: config.Placement,
		},
		Binary: config.binary(), StateDir: config.WorkingDirectory, Environment: config.Environment, Supervisor: supervisor, CommandSupervisor: config.CommandSupervisor, LaunchProcess: launchProcess,
	}), ready)
}

func runWithReady(ctx context.Context, config Config, start codexexecserver.Start, ready func(executorpb.ExecutorServer, context.Context)) (result error) {
	readinessConfig, err := config.Readiness.Resolve()
	if err != nil {
		return err
	}
	config.Readiness = readinessConfig
	config.Implementation, err = codexexecserver.ResolveImplementation(config.Implementation)
	if err != nil {
		return err
	}
	lifetime, cancel := context.WithCancel(ctx)
	defer cancel()
	instance, err := start(lifetime)
	if err != nil {
		if ctx.Err() != nil && errors.Is(err, supervision.ErrFencedLaunch) {
			// A managed launch can be canceled before returning its Instance.
			// Its owner already joined and fenced that exact process attempt.
			return nil
		}
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
	var backendConn *grpc.ClientConn
	var checkTyped codexexecserver.Probe
	if config.Implementation == codexexecserver.ImplementationNative {
		endpoint, err := url.Parse(config.ExecutorURL)
		if err != nil {
			return err
		}
		backendConn, err = grpc.NewClient("passthrough:///"+endpoint.Host, grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			return err
		}
		defer backendConn.Close()
		healthClient := healthpb.NewHealthClient(backendConn)
		checkTyped = func(ctx context.Context) error {
			result, err := healthClient.Check(ctx, &healthpb.HealthCheckRequest{})
			if err != nil {
				return err
			}
			if result.GetStatus() != healthpb.HealthCheckResponse_SERVING {
				return errors.New("typed execution backend is not serving")
			}
			return nil
		}
	}
	readiness, endReadiness := context.WithTimeout(lifetime, config.Readiness.Timeout)
	err = retry.Do(readiness, retry.NewConstant(config.Readiness.ProbeInterval), func(ctx context.Context) error {
		probeCtx, endProbe := context.WithTimeout(ctx, config.Readiness.ProbeTimeout)
		defer endProbe()
		if err := codexexecserver.ProbeAt(config.ExecutorURL)(probeCtx); err != nil {
			return retry.RetryableError(err)
		}
		if checkTyped != nil {
			if err := checkTyped(probeCtx); err != nil {
				return retry.RetryableError(err)
			}
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

	var executionAdapter interface {
		executorpb.ExecutorServer
		Close() error
	}
	if config.Implementation == codexexecserver.ImplementationCodex {
		executionAdapter, err = adapter.New(adapter.Config{
			Context: lifetime, Admit: config.admit,
			Acquire: func(context.Context) (string, error) { return config.ExecutorURL, nil },
		})
	} else {
		go monitorTypedBackend(lifetime, cancel, backendDone, checkTyped)
		executionAdapter, err = typedproxy.New(lifetime, executorpb.NewExecutorClient(backendConn), config.admit, nil)
	}
	if err != nil {
		return err
	}
	defer executionAdapter.Close()
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
		ready(executionAdapter, lifetime)
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
	if closeErr := executionAdapter.Close(); closeErr != nil {
		err = errors.Join(err, closeErr)
	}
	return err
}

func monitorTypedBackend(lifetime context.Context, cancel context.CancelFunc, backendDone chan<- error, checkTyped codexexecserver.Probe) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	failures := 0
	for {
		select {
		case <-lifetime.Done():
			return
		case <-ticker.C:
		}
		attempt, end := context.WithTimeout(lifetime, 3*time.Second)
		err := checkTyped(attempt)
		end()
		if lifetime.Err() != nil {
			return
		}
		if err == nil {
			failures = 0
			continue
		}
		failures++
		if failures >= 3 {
			select {
			case backendDone <- errors.Join(codexexecserver.ErrExecutionBackendLost, fmt.Errorf("typed execution backend health: %w", err)):
			default:
			}
			cancel()
			return
		}
	}
}

// Runtime authenticates the environment owner; Gateway authorizes remote callers. This service
// checks one immutable incarnation; it cannot adopt or replace a Runtime epoch.
func (c Config) admit(ctx context.Context, scope *executorpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
	if scope == nil || scope.Destination == nil || scope.Destination.Owner == nil || scope.SessionId == "" || len(scope.SessionId) > 128 {
		return status.Error(codes.InvalidArgument, "execution scope is incomplete")
	}
	if err := ValidateScopeBinding(scope, c.Namespace, c.SandboxName, c.SandboxUID, c.IncarnationUID); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	return accept(ctx)
}

// ValidateScopeBinding checks the request destination and expected incarnation
// against one Sandbox. FailedPrecondition means the destination matched but
// the incarnation differed. Each caller validates shape and proves its authority.
func ValidateScopeBinding(scope *executorpb.ExecutionScope, namespace, name, sandboxUID, incarnationUID string) error {
	destination := scope.GetDestination()
	target, owner := destination.GetTarget(), destination.GetOwner()
	if target.GetNamespace() == "" || target.GetName() == "" || target.GetUid() == "" ||
		target.GetNamespace() != namespace || target.GetName() != name || target.GetUid() != sandboxUID ||
		owner.GetNamespace() != namespace || owner.GetName() != name {
		return status.Error(codes.PermissionDenied, "execution destination names another Sandbox")
	}
	if scope.GetExpectedIncarnationUid() != incarnationUID {
		return status.Error(codes.FailedPrecondition, "Sandbox incarnation changed")
	}
	return nil
}

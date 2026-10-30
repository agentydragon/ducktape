package runner

import (
	"context"

	executionpb "github.com/agentydragon/ducktape/agentplane/proto/executionpb"
	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/runner/control"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// admitExecution binds an accepted open stream to its authenticated watch. Existing-session
// unary calls are admitted against the current epoch but execute under the retained local socket,
// so an RPC deadline only ends that caller's wait.
func (r *Runner) admitExecution(
	ctx context.Context,
	scope *executionpb.ExecutionScope,
	opening bool,
	accept func(context.Context) error,
) error {
	return r.control.Admit(ctx, func(epoch *control.Epoch) error {
		if scope == nil || scope.GetDestination() == nil || scope.GetDestination().GetOwner() == nil ||
			scope.GetExpectedIncarnationUid() == "" || scope.GetSessionId() == "" || len(scope.GetSessionId()) > 128 {
			return status.Error(codes.InvalidArgument, "execution scope is incomplete")
		}
		destination := scope.GetDestination()
		identity := r.config.Launch.Sandbox
		if destination.GetSandbox() != identity.Name || destination.GetSandboxUid() != identity.UID ||
			destination.GetOwner().GetNamespace() != identity.Namespace || destination.GetOwner().GetName() != identity.Name ||
			epoch.SandboxUID() != identity.UID {
			return status.Error(codes.PermissionDenied, "execution destination names another Sandbox")
		}
		if scope.GetExpectedIncarnationUid() != epoch.IncarnationUID() {
			return status.Error(codes.FailedPrecondition, "Sandbox incarnation changed")
		}

		// The opening flag identifies work admitted before the stream has a retained socket. Once
		// open, operations may continue through quiescing until the lifecycle controller drains it.
		var acceptedCtx context.Context = ctx
		var cancel context.CancelFunc
		var stopEpoch func() bool
		if opening {
			acceptedCtx, cancel = context.WithCancel(ctx)
			stopEpoch = context.AfterFunc(epoch.Context(), cancel)
			context.AfterFunc(ctx, func() {
				stopEpoch()
				cancel()
			})
		}

		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing {
			if cancel != nil {
				stopEpoch()
				cancel()
			}
			return status.Error(codes.Unavailable, "Runner is closing")
		}
		if opening {
			if !r.lifecycle.accepting() {
				stopEpoch()
				cancel()
				return status.Error(codes.FailedPrecondition, "Runner is quiescing")
			}
			if r.codexExecServer != nil {
				// Publish demand with admission before a lifecycle transition can close admission.
				if err := r.codexExecServer.RequestLocked(r.ctx); err != nil {
					stopEpoch()
					cancel()
					return status.Errorf(codes.Unavailable, "request Codex executor: %v", err)
				}
			}
		}
		if err := accept(acceptedCtx); err != nil {
			if cancel != nil {
				stopEpoch()
				cancel()
			}
			return err
		}
		return nil
	})
}

// acquireExecution waits outside admission locks for the controller-owned native endpoint.
func (r *Runner) acquireExecution(ctx context.Context) (string, error) {
	if r.codexExecServer == nil {
		return "", status.Error(codes.FailedPrecondition, "Runner has no configured Codex executor")
	}
	err := r.lifecycle.waitFor(ctx, func() (bool, error) {
		if r.lifecycle.desiredState().Mode != runnerlifecycle.Running {
			return true, status.Error(codes.FailedPrecondition, "Codex executor is stopping")
		}
		runtime, found, err := codexexecserver.ReadRuntime(ctx, r.lifecycle.state)
		if err != nil {
			return false, err
		}
		if !found {
			return false, nil
		}
		if runtime.Refusal != "" {
			return true, status.Errorf(codes.FailedPrecondition, "Codex executor refused startup: %s", runtime.Refusal)
		}
		if runtime.StopError != "" {
			return true, status.Errorf(codes.Unavailable, "Codex executor failed to stop: %s", runtime.StopError)
		}
		if runtime.Phase == codexexecserver.Ready {
			return true, nil
		}
		if runtime.Phase == codexexecserver.Stopped && runtime.Error != "" {
			return true, status.Errorf(codes.Unavailable, "Codex executor failed to start: %s", runtime.Error)
		}
		return false, nil
	})
	if err != nil {
		return "", err
	}
	return r.codexExecServer.URL(), nil
}

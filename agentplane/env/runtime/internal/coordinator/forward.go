package coordinator

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	runtimecontrol "github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/service"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type executionProxy struct {
	executorpb.UnimplementedExecutorServer
	runtime *Runtime
}

func newExecutionProxy(r *Runtime) *executionProxy { return &executionProxy{runtime: r} }

func (r *Runtime) admitExecutionCall(ctx context.Context, scope *executorpb.ExecutionScope) (executorpb.ExecutorServer, context.Context, func(), error) {
	var client executorpb.ExecutorServer
	var outgoing context.Context
	var cancel context.CancelFunc
	var callID uint64
	err := r.control.Admit(ctx, func(authorization *runtimecontrol.Authorization) error {
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing || r.failed || r.suspendPrepared || r.executionOwner.recovering() ||
			(r.desired.Mode != lifecycle.Running && r.desired.Mode != lifecycle.Quiescing) ||
			(r.desired.Mode == lifecycle.Running && !r.admissionOpen) {
			return status.Error(codes.Unavailable, "execution admission is closed")
		}
		progress, found, err := lifecycle.ReadStatus(ctx, r.lifecycleState)
		if err != nil {
			return err
		}
		if found && progress.Generation == r.desired.Generation && progress.NativeStopped {
			return status.Error(codes.FailedPrecondition, "environment native work has stopped")
		}
		if err := validateExecutionScope(scope, r.config.Launch.Sandbox, authorization.SandboxUID(), authorization.IncarnationUID()); err != nil {
			return err
		}
		client = r.executionOwner.executor(scope.ExpectedIncarnationUid)
		if client == nil {
			return status.Error(codes.Unavailable, "Execution API is not ready for this incarnation")
		}
		if err := r.changeAdmissionsLocked(1); err != nil {
			return err
		}
		outgoing, cancel = context.WithCancel(ctx)
		r.execCallID++
		callID = r.execCallID
		r.execCalls[callID] = cancel
		r.activeExecutionCalls++
		r.idleSince = nil
		r.changedLocked()
		return nil
	})
	if err != nil {
		return nil, nil, nil, err
	}
	finish := func() {
		cancel()
		r.mu.Lock()
		delete(r.execCalls, callID)
		admissionErr := r.changeAdmissionsLocked(-1)
		if r.activeExecutionCalls > 0 {
			r.activeExecutionCalls--
		}
		r.changedLocked()
		r.mu.Unlock()
		if admissionErr != nil {
			r.fail(admissionErr)
		}
	}
	return client, outgoing, finish, nil
}

func validateExecutionScope(scope *executorpb.ExecutionScope, sandbox configuration.SandboxIdentity, sandboxUID, incarnationUID string) error {
	if scope == nil || scope.Destination == nil || scope.Destination.Target == nil || scope.Destination.Owner == nil || scope.SessionId == "" || scope.ExpectedIncarnationUid == "" {
		return status.Error(codes.InvalidArgument, "execution scope is incomplete")
	}
	if sandboxUID != sandbox.UID {
		return status.Error(codes.PermissionDenied, "execution destination names another Sandbox")
	}
	err := service.ValidateScopeBinding(scope, sandbox.Namespace, sandbox.Name, sandbox.UID, incarnationUID)
	if status.Code(err) != codes.FailedPrecondition || incarnationUID == "" {
		return err
	}
	changed, err := status.Convert(err).WithDetails(&executorpb.SandboxIncarnationChanged{
		SandboxUid: sandboxUID, IncarnationUid: incarnationUID,
	})
	if err != nil {
		return err
	}
	return changed.Err()
}

type directExecutionStream struct {
	executorpb.Executor_OpenSessionServer
	ctx context.Context
}

func (s directExecutionStream) Context() context.Context { return s.ctx }

func (p *executionProxy) OpenSession(request *executorpb.OpenSessionRequest, stream executorpb.Executor_OpenSessionServer) error {
	r := p.runtime
	var client executorpb.ExecutorServer
	var streamCtx context.Context
	var cancel context.CancelFunc
	var streamID uint64
	var unconfigured bool
	err := r.control.Admit(stream.Context(), func(authorization *runtimecontrol.Authorization) error {
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing || r.failed || r.executionOwner.recovering() || !r.acceptingLocked() {
			return status.Error(codes.Unavailable, "execution admission is closed")
		}
		if err := validateExecutionScope(request.GetScope(), r.config.Launch.Sandbox, authorization.SandboxUID(), authorization.IncarnationUID()); err != nil {
			return err
		}
		client = r.executionOwner.executor(request.GetScope().ExpectedIncarnationUid)
		if client == nil {
			if r.config.CodexExecServer == nil {
				unconfigured = true
				return nil
			}
			return status.Error(codes.Unavailable, "Execution API is not ready for this incarnation")
		}
		progress, found, err := lifecycle.ReadStatus(stream.Context(), r.lifecycleState)
		if err != nil {
			return err
		}
		if found && progress.Generation == r.desired.Generation && progress.NativeStopped {
			return status.Error(codes.FailedPrecondition, "environment native work has stopped")
		}
		streamCtx, cancel = context.WithCancel(stream.Context())
		stopIdentity := context.AfterFunc(authorization.Context(), cancel)
		oldCancel := cancel
		cancel = func() { stopIdentity(); oldCancel() }
		r.execStreamID++
		streamID = r.execStreamID
		r.execStreams[streamID] = cancel
		r.activeExecutionCalls++
		r.idleSince = nil
		r.changedLocked()
		return nil
	})
	if err != nil {
		return err
	}
	if unconfigured {
		return stream.Send(&executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{
			Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED,
			Message: "Environment Runtime has no configured Codex executor",
		}})
	}
	defer func() {
		cancel()
		r.mu.Lock()
		delete(r.execStreams, streamID)
		if r.activeExecutionCalls > 0 {
			r.activeExecutionCalls--
		}
		r.changedLocked()
		r.mu.Unlock()
	}()
	return client.OpenSession(request, directExecutionStream{Executor_OpenSessionServer: stream, ctx: streamCtx})
}

// forwardExecutionUnary holds the Runtime admission until the unary Executor call returns.
func forwardExecutionUnary[Request interface {
	GetScope() *executorpb.ExecutionScope
}, Response any](
	ctx context.Context, p *executionProxy, request Request,
	call func(executorpb.ExecutorServer, context.Context, Request) (*Response, error),
) (*Response, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return call(client, outgoing, request)
}

func (p *executionProxy) StartProcess(ctx context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
	return forwardExecutionUnary(ctx, p, request, executorpb.ExecutorServer.StartProcess)
}

func (p *executionProxy) ReadProcess(ctx context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
	return forwardExecutionUnary(ctx, p, request, executorpb.ExecutorServer.ReadProcess)
}

func (p *executionProxy) WriteProcess(ctx context.Context, request *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
	return forwardExecutionUnary(ctx, p, request, executorpb.ExecutorServer.WriteProcess)
}

func (p *executionProxy) SignalProcess(ctx context.Context, request *executorpb.SignalProcessRequest) (*executorpb.SignalProcessResponse, error) {
	return forwardExecutionUnary(ctx, p, request, executorpb.ExecutorServer.SignalProcess)
}

func (p *executionProxy) TerminateProcess(ctx context.Context, request *executorpb.TerminateProcessRequest) (*executorpb.TerminateProcessResponse, error) {
	return forwardExecutionUnary(ctx, p, request, executorpb.ExecutorServer.TerminateProcess)
}

func (p *executionProxy) GetFileMetadata(ctx context.Context, request *executorpb.GetFileMetadataRequest) (*executorpb.GetFileMetadataResponse, error) {
	return forwardExecutionUnary(ctx, p, request, executorpb.ExecutorServer.GetFileMetadata)
}

func (p *executionProxy) OpenFile(ctx context.Context, request *executorpb.OpenFileRequest) (*executorpb.OpenFileResponse, error) {
	return forwardExecutionUnary(ctx, p, request, executorpb.ExecutorServer.OpenFile)
}

func (p *executionProxy) ReadFile(ctx context.Context, request *executorpb.ReadFileRequest) (*executorpb.ReadFileResponse, error) {
	return forwardExecutionUnary(ctx, p, request, executorpb.ExecutorServer.ReadFile)
}

func (p *executionProxy) CloseFile(ctx context.Context, request *executorpb.CloseFileRequest) (*executorpb.CloseFileResponse, error) {
	return forwardExecutionUnary(ctx, p, request, executorpb.ExecutorServer.CloseFile)
}

var (
	_ environmentruntimepb.EnvironmentRuntimeServer = (*Runtime)(nil)
	_ executorpb.ExecutorServer                     = (*executionProxy)(nil)
)

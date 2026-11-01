package runtime

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	runtimecontrol "github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type executionProxy struct {
	executorpb.UnimplementedExecutorServer
	runtime *Runtime
}

func newExecutionProxy(r *Runtime) *executionProxy { return &executionProxy{runtime: r} }

func (r *Runtime) authorizeHostCall(ctx context.Context, mutation bool) (*harnesshost.Runner, context.Context, func(), error) {
	var client *harnesshost.Runner
	var binding *runtimepb.Binding
	var admitted bool
	err := r.control.Admit(ctx, func(epoch *runtimecontrol.Epoch) error {
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing {
			return status.Error(codes.Unavailable, "Runner is closing")
		}
		if r.intent == nil || (r.intent.Mode != runtimepb.LifecycleIntent_RUNNING && r.intent.Mode != runtimepb.LifecycleIntent_QUIESCING) {
			return status.Error(codes.FailedPrecondition, "environment generation is not serving Host RPCs")
		}
		if r.host == nil || !r.host.connected || r.host.report == nil || !r.host.report.Ready || !proto.Equal(r.host.report.Binding, r.intent.Binding) {
			return status.Error(codes.Unavailable, "Harness Host has not applied the active environment generation")
		}
		if epoch.IncarnationUID() != r.intent.IncarnationUid {
			return status.Error(codes.Aborted, "control epoch does not match the active environment generation")
		}
		client = r.host.client
		binding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
		if mutation {
			r.activeAdmissions++
			r.idleSince = nil
			r.changedLocked()
			admitted = true
		}
		return nil
	})
	if err != nil {
		return nil, nil, nil, err
	}
	finish := func() {
		if !admitted {
			return
		}
		r.mu.Lock()
		if r.activeAdmissions > 0 {
			r.activeAdmissions--
		}
		r.changedLocked()
		r.mu.Unlock()
	}
	// The Host's ingress check runs after Runtime releases its mutex. Its
	// native admission check repeats under the Host mutex before any write.
	bound, err := client.BoundContext(ctx, binding)
	if err != nil {
		finish()
		return nil, nil, nil, err
	}
	return client, bound, finish, nil
}

// invokeHost preserves the protobuf transport's immutable request/response
// boundary while invoking the typed Host implementation in this process.
func invokeHost[Request proto.Message, Response proto.Message](r *Runtime, ctx context.Context, request Request, mutation bool, call func(*harnesshost.Runner, context.Context, Request) (Response, error)) (Response, error) {
	var zero Response
	client, bound, finish, err := r.authorizeHostCall(ctx, mutation)
	if err != nil {
		return zero, err
	}
	defer finish()
	response, err := call(client, bound, proto.Clone(request).(Request))
	if err != nil {
		return zero, err
	}
	return proto.Clone(response).(Response), nil
}

func (r *Runtime) ListSessions(ctx context.Context, request *sessionpb.ListSessionsRequest) (*sessionpb.ListSessionsResponse, error) {
	return invokeHost(r, ctx, request, false, (*harnesshost.Runner).ListSessions)
}

func (r *Runtime) GetStatus(ctx context.Context, request *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	response, err := invokeHost(r, ctx, request, false, (*harnesshost.Runner).GetStatus)
	if err != nil {
		return nil, err
	}
	// The public boot ID fences the Gateway epoch; Host's private boot ID
	// stays inside its lifecycle binding.
	response.EnvironmentRuntimeBootId = r.bootID
	return response, nil
}

func (r *Runtime) OpenSession(ctx context.Context, request *environmentruntimepb.OpenSessionRequest) (*environmentruntimepb.OpenSessionResponse, error) {
	return invokeHost(r, ctx, request, true, (*harnesshost.Runner).OpenSession)
}

func (r *Runtime) SubmitInput(ctx context.Context, request *environmentruntimepb.SubmitInputRequest) (*environmentruntimepb.SubmitInputResponse, error) {
	return invokeHost(r, ctx, request, true, (*harnesshost.Runner).SubmitInput)
}

func (r *Runtime) ChangeModel(ctx context.Context, request *environmentruntimepb.ChangeModelRequest) (*environmentruntimepb.ChangeModelResponse, error) {
	return invokeHost(r, ctx, request, true, (*harnesshost.Runner).ChangeModel)
}

func (r *Runtime) ChangeReasoningEffort(ctx context.Context, request *environmentruntimepb.ChangeReasoningEffortRequest) (*environmentruntimepb.ChangeReasoningEffortResponse, error) {
	return invokeHost(r, ctx, request, true, (*harnesshost.Runner).ChangeReasoningEffort)
}

func (r *Runtime) InterruptTurn(ctx context.Context, request *environmentruntimepb.InterruptTurnRequest) (*environmentruntimepb.InterruptTurnResponse, error) {
	return invokeHost(r, ctx, request, true, (*harnesshost.Runner).InterruptTurn)
}

func (r *Runtime) admitExecutionCall(ctx context.Context, scope *executorpb.ExecutionScope) (executorpb.ExecutorServer, context.Context, func(), error) {
	var client executorpb.ExecutorServer
	var outgoing context.Context
	var cancel context.CancelFunc
	var callID uint64
	var admitted bool
	err := r.control.Admit(ctx, func(epoch *runtimecontrol.Epoch) error {
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing || r.executionRecovering || r.intent == nil || (r.intent.Mode != runtimepb.LifecycleIntent_RUNNING && r.intent.Mode != runtimepb.LifecycleIntent_QUIESCING) {
			return status.Error(codes.Unavailable, "execution admission is closed")
		}
		if r.host != nil && r.host.report != nil && r.host.report.NativeStopped && proto.Equal(r.host.report.Binding, r.intent.Binding) {
			return status.Error(codes.FailedPrecondition, "environment native work has stopped")
		}
		if err := validateExecutionScope(scope, r.config.Launch.Sandbox, epoch.SandboxUID(), epoch.IncarnationUID()); err != nil {
			return err
		}
		if r.executionServer == nil || r.executionUID != scope.ExpectedIncarnationUid {
			return status.Error(codes.Unavailable, "Execution API is not ready for this incarnation")
		}
		client = r.executionServer
		outgoing, cancel = context.WithCancel(ctx)
		r.execCallID++
		callID = r.execCallID
		r.execCalls[callID] = cancel
		r.activeAdmissions++
		r.activeExecutionCalls++
		r.idleSince = nil
		r.changedLocked()
		admitted = true
		return nil
	})
	if err != nil {
		return nil, nil, nil, err
	}
	finish := func() {
		if !admitted {
			return
		}
		cancel()
		r.mu.Lock()
		delete(r.execCalls, callID)
		if r.activeAdmissions > 0 {
			r.activeAdmissions--
		}
		if r.activeExecutionCalls > 0 {
			r.activeExecutionCalls--
		}
		r.changedLocked()
		r.mu.Unlock()
	}
	return client, outgoing, finish, nil
}

func validateExecutionScope(scope *executorpb.ExecutionScope, sandbox configuration.SandboxIdentity, sandboxUID, incarnationUID string) error {
	if scope == nil || scope.Destination == nil || scope.Destination.Owner == nil || scope.SessionId == "" || scope.ExpectedIncarnationUid == "" {
		return status.Error(codes.InvalidArgument, "execution scope is incomplete")
	}
	destination := scope.Destination
	if destination.Sandbox != sandbox.Name || destination.SandboxUid != sandbox.UID || destination.Owner.Namespace != sandbox.Namespace || destination.Owner.Name != sandbox.Name || sandboxUID != sandbox.UID {
		return status.Error(codes.PermissionDenied, "execution destination names another Sandbox")
	}
	if scope.ExpectedIncarnationUid != incarnationUID {
		if sandbox.UID != "" && sandboxUID == sandbox.UID && incarnationUID != "" {
			changed, err := status.New(codes.FailedPrecondition, "Sandbox incarnation changed").WithDetails(
				&executorpb.SandboxIncarnationChanged{
					SandboxUid: sandbox.UID, IncarnationUid: incarnationUID,
				},
			)
			if err != nil {
				return err
			}
			return changed.Err()
		}
		return status.Error(codes.FailedPrecondition, "Sandbox incarnation changed")
	}
	return nil
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
	err := r.control.Admit(stream.Context(), func(epoch *runtimecontrol.Epoch) error {
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing || r.executionRecovering || r.intent == nil || r.intent.Mode != runtimepb.LifecycleIntent_RUNNING {
			return status.Error(codes.Unavailable, "execution admission is closed")
		}
		if err := validateExecutionScope(request.GetScope(), r.config.Launch.Sandbox, epoch.SandboxUID(), epoch.IncarnationUID()); err != nil {
			return err
		}
		if r.executionServer == nil || r.executionUID != request.GetScope().ExpectedIncarnationUid {
			if r.config.CodexExecServer == nil {
				unconfigured = true
				return nil
			}
			return status.Error(codes.Unavailable, "Execution API is not ready for this incarnation")
		}
		if r.host != nil && r.host.report != nil && r.host.report.NativeStopped && proto.Equal(r.host.report.Binding, r.intent.Binding) {
			return status.Error(codes.FailedPrecondition, "environment native work has stopped")
		}
		client = r.executionServer
		streamCtx, cancel = context.WithCancel(stream.Context())
		stopEpoch := context.AfterFunc(epoch.Context(), cancel)
		oldCancel := cancel
		cancel = func() { stopEpoch(); oldCancel() }
		r.execStreamID++
		streamID = r.execStreamID
		r.execStreams[streamID] = cancel
		r.activeExecStreams++
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
			Message: "Runner has no configured Codex executor",
		}})
	}
	defer func() {
		cancel()
		r.mu.Lock()
		delete(r.execStreams, streamID)
		if r.activeExecStreams > 0 {
			r.activeExecStreams--
		}
		if r.activeExecutionCalls > 0 {
			r.activeExecutionCalls--
		}
		r.changedLocked()
		r.mu.Unlock()
	}()
	return client.OpenSession(request, directExecutionStream{Executor_OpenSessionServer: stream, ctx: streamCtx})
}

func (p *executionProxy) StartProcess(ctx context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return client.StartProcess(outgoing, request)
}

func (p *executionProxy) ReadProcess(ctx context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return client.ReadProcess(outgoing, request)
}

func (p *executionProxy) WriteProcess(ctx context.Context, request *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return client.WriteProcess(outgoing, request)
}

func (p *executionProxy) SignalProcess(ctx context.Context, request *executorpb.SignalProcessRequest) (*executorpb.SignalProcessResponse, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return client.SignalProcess(outgoing, request)
}

func (p *executionProxy) TerminateProcess(ctx context.Context, request *executorpb.TerminateProcessRequest) (*executorpb.TerminateProcessResponse, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return client.TerminateProcess(outgoing, request)
}

func (p *executionProxy) GetFileMetadata(ctx context.Context, request *executorpb.GetFileMetadataRequest) (*executorpb.GetFileMetadataResponse, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return client.GetFileMetadata(outgoing, request)
}

func (p *executionProxy) OpenFile(ctx context.Context, request *executorpb.OpenFileRequest) (*executorpb.OpenFileResponse, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return client.OpenFile(outgoing, request)
}

func (p *executionProxy) ReadFile(ctx context.Context, request *executorpb.ReadFileRequest) (*executorpb.ReadFileResponse, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return client.ReadFile(outgoing, request)
}

func (p *executionProxy) CloseFile(ctx context.Context, request *executorpb.CloseFileRequest) (*executorpb.CloseFileResponse, error) {
	client, outgoing, finish, err := p.runtime.admitExecutionCall(ctx, request.GetScope())
	if err != nil {
		return nil, err
	}
	defer finish()
	return client.CloseFile(outgoing, request)
}

var (
	_ environmentruntimepb.EnvironmentRuntimeServer = (*Runtime)(nil)
	_ executorpb.ExecutorServer                     = (*executionProxy)(nil)
)

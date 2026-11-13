package gateway

import (
	"context"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

var identityMetadataKeys = []string{
	sandboxapi.RunnerProxySandboxNamespaceMetadataKey,
	sandboxapi.RunnerProxySandboxUIDMetadataKey,
	sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey,
	sandboxapi.RunnerProxySandboxOwnerNameMetadataKey,
}

type environmentRuntimeProxy struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	gateway *Gateway
}

// connection is the elected connection to the runner of the Sandbox the call's metadata names. Gateway.Methods
// admitted the caller.
func (p *environmentRuntimeProxy) connection(ctx context.Context, writes, allowSuspending bool) (*runnerConnection, error) {
	g := p.gateway
	metadata, _ := metadata.FromIncomingContext(ctx)
	names := metadata.Get(sandboxapi.RunnerProxySandboxMetadataKey)
	if len(names) != 1 || names[0] == "" {
		return nil, status.Errorf(codes.InvalidArgument, "exactly one %s header is required", sandboxapi.RunnerProxySandboxMetadataKey)
	}
	values := make(map[string][]string, len(identityMetadataKeys))
	for _, key := range identityMetadataKeys {
		values[key] = metadata.Get(key)
	}
	for _, key := range identityMetadataKeys {
		if len(values[key]) != 1 || values[key][0] == "" {
			return nil, status.Error(codes.InvalidArgument, "Sandbox UID and owner headers must each appear exactly once")
		}
	}
	view, err := g.sandboxes.destination(ctx, &identitypb.SandboxDestination{
		Target: &identitypb.EnvironmentTarget{
			Namespace: values[sandboxapi.RunnerProxySandboxNamespaceMetadataKey][0],
			Name:      names[0], Uid: values[sandboxapi.RunnerProxySandboxUIDMetadataKey][0],
		},
		Owner: &identitypb.ServiceAccount{
			Namespace: values[sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey][0],
			Name:      values[sandboxapi.RunnerProxySandboxOwnerNameMetadataKey][0],
		},
	}, writes, allowSuspending)
	if err != nil {
		return nil, unavailable(err)
	}
	connection, err := g.connections.holder(view.identity.Uid)
	if err != nil {
		return nil, err
	}
	if view.incarnation == nil || !proto.Equal(connection.identity, view.identity) || connection.podUID != view.incarnation.UID {
		return nil, status.Error(codes.Unavailable, "runner connection belongs to another incarnation")
	}
	return connection, nil
}

func (p *environmentRuntimeProxy) ListSessions(ctx context.Context, request *sessionpb.ListSessionsRequest) (*sessionpb.ListSessionsResponse, error) {
	connection, err := p.connection(ctx, false, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.ListSessions(ctx, request)
}

func (p *environmentRuntimeProxy) GetStatus(ctx context.Context, request *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	connection, err := p.connection(ctx, false, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.GetStatus(ctx, request)
}

func (p *environmentRuntimeProxy) CreateSession(ctx context.Context, request *environmentruntimepb.CreateSessionRequest) (*environmentruntimepb.CreateSessionResponse, error) {
	connection, err := p.connection(ctx, true, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.CreateSession(ctx, request)
}

func (p *environmentRuntimeProxy) Quiesce(ctx context.Context, request *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
	// The controller may have published Quiescing while this Gateway still caches the preceding
	// SuspendPending phase. Only this controller-authorized control call can use that phase.
	connection, err := p.connection(ctx, false, true)
	if err != nil {
		return nil, err
	}
	return connection.runner.Quiesce(ctx, request)
}

func (p *environmentRuntimeProxy) PrepareSuspend(ctx context.Context, request *environmentruntimepb.PrepareSuspendRequest) (*emptypb.Empty, error) {
	connection, err := p.connection(ctx, false, true)
	if err != nil {
		return nil, err
	}
	return connection.runner.PrepareSuspend(ctx, request)
}

func (p *environmentRuntimeProxy) ResumeAdmission(ctx context.Context, request *environmentruntimepb.ResumeAdmissionRequest) (*emptypb.Empty, error) {
	connection, err := p.connection(ctx, false, true)
	if err != nil {
		return nil, err
	}
	return connection.runner.ResumeAdmission(ctx, request)
}

func (p *environmentRuntimeProxy) SubmitInput(ctx context.Context, request *environmentruntimepb.SubmitInputRequest) (*environmentruntimepb.SubmitInputResponse, error) {
	connection, err := p.connection(ctx, true, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.SubmitInput(ctx, request)
}

func (p *environmentRuntimeProxy) ChangeModel(ctx context.Context, request *environmentruntimepb.ChangeModelRequest) (*environmentruntimepb.ChangeModelResponse, error) {
	connection, err := p.connection(ctx, true, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.ChangeModel(ctx, request)
}

func (p *environmentRuntimeProxy) ChangeReasoningEffort(ctx context.Context, request *environmentruntimepb.ChangeReasoningEffortRequest) (*environmentruntimepb.ChangeReasoningEffortResponse, error) {
	connection, err := p.connection(ctx, true, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.ChangeReasoningEffort(ctx, request)
}

func (p *environmentRuntimeProxy) InterruptTurn(ctx context.Context, request *environmentruntimepb.InterruptTurnRequest) (*environmentruntimepb.InterruptTurnResponse, error) {
	connection, err := p.connection(ctx, true, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.InterruptTurn(ctx, request)
}

var _ environmentruntimepb.EnvironmentRuntimeServer = (*environmentRuntimeProxy)(nil)

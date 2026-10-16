package gateway

import (
	"context"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	sandboxHeader               = "x-agentplane-sandbox"
	sandboxUIDHeader            = "x-agentplane-sandbox-uid"
	sandboxOwnerNamespaceHeader = "x-agentplane-sandbox-owner-namespace"
	sandboxOwnerNameHeader      = "x-agentplane-sandbox-owner-name"
)

var identityHeaders = []string{sandboxUIDHeader, sandboxOwnerNamespaceHeader, sandboxOwnerNameHeader}

type runnerProxy struct {
	runnercontrolpb.UnimplementedRunnerServer
	gateway *Gateway
}

func (p *runnerProxy) connection(ctx context.Context, controllerOnly bool) (*runnerConnection, error) {
	g := p.gateway
	allowed := g.callers
	if controllerOnly {
		allowed = make(map[auth.Account]struct{})
		if g.controller != nil {
			allowed[*g.controller] = struct{}{}
		}
	}
	if err := g.authorize(ctx, allowed); err != nil {
		return nil, err
	}
	metadata, _ := metadata.FromIncomingContext(ctx)
	names := metadata.Get(sandboxHeader)
	if len(names) != 1 || names[0] == "" {
		return nil, status.Error(codes.InvalidArgument, "exactly one x-agentplane-sandbox header is required")
	}
	values := make(map[string][]string, len(identityHeaders))
	for _, header := range identityHeaders {
		values[header] = metadata.Get(header)
	}
	for _, header := range identityHeaders {
		if len(values[header]) != 1 || values[header][0] == "" {
			return nil, status.Error(codes.InvalidArgument, "Sandbox UID and owner headers must each appear exactly once")
		}
	}
	view, err := g.sandboxes.destination(ctx, &sandboxespb.SandboxDestination{
		Sandbox: names[0], SandboxUid: values[sandboxUIDHeader][0],
		Owner: &sandboxespb.ServiceAccount{
			Namespace: values[sandboxOwnerNamespaceHeader][0], Name: values[sandboxOwnerNameHeader][0],
		},
	}, false)
	if err != nil {
		return nil, unavailable(err)
	}
	return g.connections.holder(view.identity.UID)
}

func (p *runnerProxy) ListSessions(ctx context.Context, request *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
	connection, err := p.connection(ctx, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.ListSessions(ctx, request)
}

func (p *runnerProxy) OpenSession(ctx context.Context, request *runnercontrolpb.OpenSessionRequest) (*runnercontrolpb.OpenSessionResponse, error) {
	connection, err := p.connection(ctx, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.OpenSession(ctx, request)
}

func (p *runnerProxy) ResumeSession(ctx context.Context, request *runnercontrolpb.ResumeSessionRequest) (*runnercontrolpb.ResumeSessionResponse, error) {
	connection, err := p.connection(ctx, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.ResumeSession(ctx, request)
}

func (p *runnerProxy) Quiesce(ctx context.Context, request *runnerpb.QuiesceRequest) (*runnerpb.QuiesceResponse, error) {
	connection, err := p.connection(ctx, true)
	if err != nil {
		return nil, err
	}
	return connection.runner.Quiesce(ctx, request)
}

func (p *runnerProxy) SubmitInput(ctx context.Context, request *runnercontrolpb.SubmitInputRequest) (*runnercontrolpb.SubmitInputResponse, error) {
	connection, err := p.connection(ctx, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.SubmitInput(ctx, request)
}

func (p *runnerProxy) ChangeModel(ctx context.Context, request *runnercontrolpb.ChangeModelRequest) (*runnercontrolpb.ChangeModelResponse, error) {
	connection, err := p.connection(ctx, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.ChangeModel(ctx, request)
}

func (p *runnerProxy) ChangeReasoningEffort(ctx context.Context, request *runnercontrolpb.ChangeReasoningEffortRequest) (*runnercontrolpb.ChangeReasoningEffortResponse, error) {
	connection, err := p.connection(ctx, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.ChangeReasoningEffort(ctx, request)
}

func (p *runnerProxy) InterruptTurn(ctx context.Context, request *runnercontrolpb.InterruptTurnRequest) (*runnercontrolpb.InterruptTurnResponse, error) {
	connection, err := p.connection(ctx, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.InterruptTurn(ctx, request)
}

func (p *runnerProxy) StopRunnerSession(ctx context.Context, request *runnercontrolpb.StopRunnerSessionRequest) (*runnercontrolpb.StopRunnerSessionResponse, error) {
	connection, err := p.connection(ctx, false)
	if err != nil {
		return nil, err
	}
	return connection.runner.StopRunnerSession(ctx, request)
}

var _ runnercontrolpb.RunnerServer = (*runnerProxy)(nil)

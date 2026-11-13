package bridge

import (
	"context"
	"errors"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/environmentruntimeclient"
)

// GatewayRunnerProxy invokes informational Runner RPCs through the named
// Sandbox's control Service and supplies its current identity on every call.
type GatewayRunnerProxy struct{ config RunnerConfig }

// NewGatewayRunnerProxy validates the per-Sandbox runner transport settings.
func NewGatewayRunnerProxy(config RunnerConfig) (*GatewayRunnerProxy, error) {
	if config.Namespace == "" || config.TokenFile == "" || config.Timeout <= 0 {
		return nil, errors.New("runner namespace, token file, and positive timeout are required")
	}
	if config.TargetTemplate == "" {
		config.TargetTemplate = sandboxapi.ControlServiceTargetTemplate()
	}
	return &GatewayRunnerProxy{config: config}, nil
}

func (r *GatewayRunnerProxy) withClient(ctx context.Context, sandbox *environmentpb.EnvironmentDescriptor, call func(context.Context, environmentruntimepb.EnvironmentRuntimeClient) error) error {
	targetHandle := sandbox.GetTarget()
	if targetHandle == nil || targetHandle.GetName() == "" || targetHandle.GetUid() == "" || targetHandle.GetNamespace() == "" || targetHandle.GetNamespace() != r.config.Namespace {
		return errors.New("complete Sandbox identity in the configured namespace is required")
	}
	target := environmentruntimeclient.ExpandTarget(r.config.TargetTemplate, targetHandle.GetName(), targetHandle.GetNamespace())
	err := environmentruntimeclient.WithClient(ctx, environmentruntimeclient.Config{
		Target: target, TokenFile: r.config.TokenFile, TLS: r.config.TLS, Timeout: r.config.Timeout,
		SandboxName: targetHandle.GetName(), SandboxUID: targetHandle.GetUid(), SandboxNamespace: targetHandle.GetNamespace(),
		ReplaceOutgoingMetadata: true,
	}, call)
	return translatedRPCError(err)
}

// ListSessions returns all durable Sessions retained by the connected runner.
func (r *GatewayRunnerProxy) ListSessions(ctx context.Context, sandbox *environmentpb.EnvironmentDescriptor) (*sessionpb.ListSessionsResponse, error) {
	var response *sessionpb.ListSessionsResponse
	err := r.withClient(ctx, sandbox, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		var err error
		response, err = client.ListSessions(ctx, &sessionpb.ListSessionsRequest{})
		return err
	})
	return response, err
}

// EnvironmentRuntimeStatus returns the live informational status of the connected runner.
func (r *GatewayRunnerProxy) EnvironmentRuntimeStatus(ctx context.Context, sandbox *environmentpb.EnvironmentDescriptor) (*sessionpb.EnvironmentRuntimeStatus, error) {
	var response *sessionpb.EnvironmentRuntimeStatus
	err := r.withClient(ctx, sandbox, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		var err error
		response, err = client.GetStatus(ctx, &sessionpb.GetStatusRequest{})
		return err
	})
	return response, err
}

var _ RunnerProxy = (*GatewayRunnerProxy)(nil)

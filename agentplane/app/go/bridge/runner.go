package bridge

import (
	"context"
	"errors"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runnerclient"
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

func (r *GatewayRunnerProxy) withClient(ctx context.Context, sandbox *sandboxespb.Sandbox, call func(context.Context, runnerservicepb.RunnerClient) error) error {
	if sandbox == nil || sandbox.GetName() == "" || sandbox.GetUid() == "" || sandbox.GetNamespace() == "" || sandbox.GetNamespace() != r.config.Namespace {
		return errors.New("complete Sandbox identity in the configured namespace is required")
	}
	target := runnerclient.ExpandTarget(r.config.TargetTemplate, sandbox.GetName(), sandbox.GetNamespace())
	err := runnerclient.WithClient(ctx, runnerclient.Config{
		Target: target, TokenFile: r.config.TokenFile, Timeout: r.config.Timeout,
		SandboxName: sandbox.GetName(), SandboxUID: sandbox.GetUid(), SandboxNamespace: sandbox.GetNamespace(),
		ReplaceOutgoingMetadata: true,
	}, call)
	return translatedRPCError(err)
}

// ListSessions returns all durable Sessions retained by the connected runner.
func (r *GatewayRunnerProxy) ListSessions(ctx context.Context, sandbox *sandboxespb.Sandbox) (*runnerpb.ListSessionsResponse, error) {
	var response *runnerpb.ListSessionsResponse
	err := r.withClient(ctx, sandbox, func(ctx context.Context, client runnerservicepb.RunnerClient) error {
		var err error
		response, err = client.ListSessions(ctx, &runnerpb.ListSessionsRequest{})
		return err
	})
	return response, err
}

// RunnerStatus returns the live informational status of the connected runner.
func (r *GatewayRunnerProxy) RunnerStatus(ctx context.Context, sandbox *sandboxespb.Sandbox) (*runnerpb.RunnerStatus, error) {
	var response *runnerpb.RunnerStatus
	err := r.withClient(ctx, sandbox, func(ctx context.Context, client runnerservicepb.RunnerClient) error {
		var err error
		response, err = client.GetStatus(ctx, &runnerpb.GetStatusRequest{})
		return err
	})
	return response, err
}

var _ RunnerProxy = (*GatewayRunnerProxy)(nil)

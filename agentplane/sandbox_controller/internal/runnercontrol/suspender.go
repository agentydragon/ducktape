package runnercontrol

import (
	"context"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/environmentruntimeclient"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// GatewaySuspender fences Runtime admission through the Runner Gateway. Generations survive
// controller restarts and distinguish delayed calls from a later attempt.
type GatewaySuspender struct {
	TLS       *peertls.ClientTLS
	Target    string
	TokenFile string
}

func (g GatewaySuspender) withClient(ctx context.Context, sandbox *sandboxapi.Sandbox, call func(context.Context, environmentruntimepb.EnvironmentRuntimeClient) error) error {
	return environmentruntimeclient.WithClient(ctx, environmentruntimeclient.Config{
		Target:    environmentruntimeclient.ExpandTarget(g.Target, sandbox.Name, sandbox.Namespace),
		TokenFile: g.TokenFile, TLS: g.TLS, Timeout: quiesceTimeout,
		SandboxName: sandbox.Name, SandboxUID: string(sandbox.UID), SandboxNamespace: sandbox.Namespace,
	}, call)
}

func (g GatewaySuspender) Prepare(ctx context.Context, sandbox *sandboxapi.Sandbox, generation int64, idleSince time.Time) error {
	return g.withClient(ctx, sandbox, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		_, err := client.PrepareSuspend(ctx, &environmentruntimepb.PrepareSuspendRequest{
			Generation: generation, IfIdleSince: timestamppb.New(idleSince),
		})
		return err
	})
}

func (g GatewaySuspender) Resume(ctx context.Context, sandbox *sandboxapi.Sandbox, generation int64) error {
	return g.withClient(ctx, sandbox, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		_, err := client.ResumeAdmission(ctx, &environmentruntimepb.ResumeAdmissionRequest{Generation: generation})
		return err
	})
}

package runnercontrol

import (
	"context"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"

	"github.com/agentydragon/ducktape/agentplane/observability"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/environmentruntimeclient"
	"go.opentelemetry.io/otel/attribute"

	"google.golang.org/protobuf/types/known/timestamppb"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxcontroller "github.com/agentydragon/ducktape/agentplane/sandbox_controller/internal/controller"
	"google.golang.org/grpc/status"
)

// quiesceTimeout bounds one Runner Gateway call; stopping harnesses can take a while, and Quiesce is
// idempotent, so a call that runs out of time is repeated.
const quiesceTimeout = 30 * time.Second

// GatewayQuiescer calls `Runner.Quiesce` through the Runner Gateway's proxy, at the Sandbox's own
// control Service, which names the replica holding its runner connection.
type GatewayQuiescer struct {
	TLS *peertls.ClientTLS
	// Target is a template of `{sandbox}` and `{namespace}`, typically
	// sandboxapi.ControlServiceTargetTemplate.
	Target string
	// TokenFile is the projected ServiceAccount token the gateway accepts from this controller.
	TokenFile string
}

func (g GatewayQuiescer) Quiesce(ctx context.Context, sandbox *sandboxapi.Sandbox, ifIdleSince *time.Time) (sandboxcontroller.QuiesceResult, error) {
	target := environmentruntimeclient.ExpandTarget(g.Target, sandbox.Name, sandbox.Namespace)
	request := &sessionpb.QuiesceRequest{}
	if ifIdleSince != nil {
		request.IfIdleSince = timestamppb.New(*ifIdleSince)
	}
	var response *sessionpb.QuiesceResponse
	called := false
	err := environmentruntimeclient.WithClient(ctx, environmentruntimeclient.Config{
		Target: target, TokenFile: g.TokenFile, TLS: g.TLS, Timeout: quiesceTimeout,
		SandboxName: sandbox.Name, SandboxUID: string(sandbox.UID), SandboxNamespace: sandbox.Namespace,
	}, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		called = true
		callCtx, span := observability.Start(ctx, "agentplane.sandbox_controller.runner_quiesce")
		span.SetAttributes(
			attribute.String("sandbox.name", sandbox.Name),
			attribute.String("sandbox.uid", string(sandbox.UID)),
		)
		var callErr error
		response, callErr = client.Quiesce(callCtx, request)
		span.SetAttributes(attribute.Int("rpc.grpc.status_code", int(status.Code(callErr))))
		observability.End(span, callErr)
		return callErr
	})
	if err != nil {
		if called {
			return sandboxcontroller.QuiesceResult{}, fmt.Errorf("quiescing %s through %s: %w", sandbox.Name, target, err)
		}
		return sandboxcontroller.QuiesceResult{}, err
	}
	return quiesceResult(response), nil
}

func quiesceResult(response *sessionpb.QuiesceResponse) sandboxcontroller.QuiesceResult {
	return sandboxcontroller.QuiesceResult{Quiescing: response.GetStage() != sessionpb.QuiesceStage_QUIESCE_STAGE_QUIESCED}
}

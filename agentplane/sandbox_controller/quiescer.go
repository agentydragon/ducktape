package sandboxcontroller

import (
	"context"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runnerclient"
	"go.opentelemetry.io/otel/attribute"

	"google.golang.org/protobuf/types/known/timestamppb"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"google.golang.org/grpc/status"
)

// quiesceTimeout bounds one Quiesce call; stopping harnesses can take a while, and Quiesce is idempotent, so
// a call that runs out of time is repeated.
const quiesceTimeout = 30 * time.Second

// QuiesceResult is a runner's answer to a quiesce.
type QuiesceResult struct {
	// True unless the runner explicitly reports QUIESCE_STAGE_QUIESCED. An absent or unknown stage is
	// not final.
	Quiescing bool
}

// Quiescer asks a Sandbox's runner to seal every Session. A non-nil ifIdleSince makes the request
// conditional: a runner that is busy, or idle since another instant, refuses with `ABORTED` instead of
// beginning to quiesce.
type Quiescer interface {
	Quiesce(ctx context.Context, sandbox *sandboxapi.Sandbox, ifIdleSince *time.Time) (QuiesceResult, error)
}

// GatewayQuiescer calls `Runner.Quiesce` through the Runner Gateway's proxy, at the Sandbox's own
// control Service, which names the replica holding its runner connection.
type GatewayQuiescer struct {
	// Target is a template of `{sandbox}` and `{namespace}`, typically
	// sandboxapi.ControlServiceTargetTemplate.
	Target string
	// TokenFile is the projected ServiceAccount token the gateway accepts from this controller.
	TokenFile string
}

func (g GatewayQuiescer) Quiesce(ctx context.Context, sandbox *sandboxapi.Sandbox, ifIdleSince *time.Time) (QuiesceResult, error) {
	target := runnerclient.ExpandTarget(g.Target, sandbox.Name, sandbox.Namespace)
	request := &runnerpb.QuiesceRequest{}
	if ifIdleSince != nil {
		request.IfIdleSince = timestamppb.New(*ifIdleSince)
	}
	var response *runnerpb.QuiesceResponse
	called := false
	err := runnerclient.WithClient(ctx, runnerclient.Config{
		Target: target, TokenFile: g.TokenFile, Timeout: quiesceTimeout,
		SandboxName: sandbox.Name, SandboxUID: string(sandbox.UID), SandboxNamespace: sandbox.Namespace,
	}, func(ctx context.Context, client runnerservicepb.RunnerClient) error {
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
			return QuiesceResult{}, fmt.Errorf("quiescing %s through %s: %w", sandbox.Name, target, err)
		}
		return QuiesceResult{}, err
	}
	return quiesceResult(response), nil
}

func quiesceResult(response *runnerpb.QuiesceResponse) QuiesceResult {
	return QuiesceResult{Quiescing: response.GetStage() != runnerpb.QuiesceStage_QUIESCE_STAGE_QUIESCED}
}

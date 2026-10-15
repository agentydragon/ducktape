package sandboxcontroller

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
)

// quiesceTimeout bounds one Seal call; stopping harnesses can take a while, and Seal is idempotent, so
// a call that runs out of time is repeated.
const quiesceTimeout = 30 * time.Second

// QuiesceResult is a runner's answer to a quiesce.
type QuiesceResult struct {
	// True unless the runner explicitly reports QUIESCE_STAGE_QUIESCED. An absent or unknown stage is
	// not final.
	Quiescing bool
}

// Quiescer asks a Sandbox's runner to seal every Session.
type Quiescer interface {
	Quiesce(ctx context.Context, sandbox *Sandbox) (QuiesceResult, error)
}

// GatewayQuiescer calls `Runner.Quiesce` through the Runner Gateway's proxy, at the Sandbox's own
// control Service, which names the replica holding its runner connection.
type GatewayQuiescer struct {
	// Target is a template of `{sandbox}` and `{namespace}`: `{sandbox}-control.{namespace}.svc:8080`.
	Target string
	// TokenFile is the projected ServiceAccount token the gateway accepts from this controller.
	TokenFile string
}

func (g GatewayQuiescer) Quiesce(ctx context.Context, sandbox *Sandbox) (QuiesceResult, error) {
	// Projected tokens rotate: read it for every call.
	token, err := os.ReadFile(g.TokenFile)
	if err != nil {
		return QuiesceResult{}, err
	}
	target := strings.NewReplacer("{sandbox}", sandbox.Name, "{namespace}", sandbox.Namespace).Replace(g.Target)
	// A connection for this call alone: a replica that no longer holds the runner answers UNAVAILABLE,
	// and the retry resolves the control Service again.
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return QuiesceResult{}, err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, quiesceTimeout)
	defer cancel()
	ctx = metadata.AppendToOutgoingContext(
		ctx,
		"authorization", "Bearer "+strings.TrimSpace(string(token)),
		"x-agentplane-sandbox", sandbox.Name,
	)
	response, err := runnercontrolpb.NewRunnerClient(conn).Quiesce(ctx, &runnerpb.QuiesceRequest{})
	if err != nil {
		return QuiesceResult{}, fmt.Errorf("quiescing %s through %s: %w", sandbox.Name, target, err)
	}
	return quiesceResult(response), nil
}

func quiesceResult(response *runnerpb.QuiesceResponse) QuiesceResult {
	// GetStage returns UNSPECIFIED when the field is absent. That response shape predates the
	// nonblocking protocol and cannot prove completion; do not infer it from the legacy sessions list.
	return QuiesceResult{Quiescing: response.GetStage() != runnerpb.QuiesceStage_QUIESCE_STAGE_QUIESCED}
}

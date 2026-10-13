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

// sealTimeout bounds one Seal call; stopping harnesses can take a while, and Seal is idempotent, so
// a call that runs out of time is repeated.
const sealTimeout = 30 * time.Second

// SealResult is a runner's answer to a seal.
type SealResult struct {
	Sessions int
	// Sessions the runner's ingress has not acknowledged the seal of yet.
	Unflushed []string
}

// Sealer asks a Sandbox's runner to seal every Session.
type Sealer interface {
	Seal(ctx context.Context, sandbox *Sandbox) (SealResult, error)
}

// GatewaySealer calls `Runner.Quiesce` through the Runner Gateway's proxy, at the Sandbox's own
// control Service, which names the replica holding its runner connection.
type GatewaySealer struct {
	// Target is a template of `{sandbox}` and `{namespace}`: `{sandbox}-control.{namespace}.svc:8080`.
	Target string
	// TokenFile is the projected ServiceAccount token the gateway accepts from this controller.
	TokenFile string
}

func (g GatewaySealer) Seal(ctx context.Context, sandbox *Sandbox) (SealResult, error) {
	// Projected tokens rotate: read it for every call.
	token, err := os.ReadFile(g.TokenFile)
	if err != nil {
		return SealResult{}, err
	}
	target := strings.NewReplacer("{sandbox}", sandbox.Name, "{namespace}", sandbox.Namespace).Replace(g.Target)
	// A connection for this call alone: a replica that no longer holds the runner answers UNAVAILABLE,
	// and the retry resolves the control Service again.
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return SealResult{}, err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, sealTimeout)
	defer cancel()
	ctx = metadata.AppendToOutgoingContext(
		ctx,
		"authorization", "Bearer "+strings.TrimSpace(string(token)),
		"x-agentplane-sandbox", sandbox.Name,
	)
	response, err := runnercontrolpb.NewRunnerClient(conn).Quiesce(ctx, &runnerpb.QuiesceRequest{})
	if err != nil {
		return SealResult{}, fmt.Errorf("quiescing %s through %s: %w", sandbox.Name, target, err)
	}
	result := SealResult{Sessions: len(response.GetSessions())}
	for _, session := range response.GetSessions() {
		if !session.GetFlushed() {
			result.Unflushed = append(result.Unflushed, session.GetSessionId())
		}
	}
	return result, nil
}

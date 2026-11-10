package runtime

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	"github.com/agentydragon/ducktape/agentplane/observability"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func (c *Config) resolveSessionReconcileConcurrency() error {
	if c.SessionReconcileConcurrency < 0 {
		return errors.New("session reconcile concurrency must be positive")
	}
	if c.SessionReconcileConcurrency == 0 {
		c.SessionReconcileConcurrency = configuration.DefaultSessionReconcileConcurrency
	}
	return nil
}

// ResolveModelConfig returns the context budget the LLM ingress configures for `model`, or nil for none.
// Codex has no budget here: it reads its context window from the model catalog its provider names.
func (c Config) ResolveModelConfig(ctx context.Context, harness sessionpb.Harness, model string) (*int, error) {
	launch := c.Claude
	if harness == sessionpb.Harness_HARNESS_CODEX {
		launch = c.Codex
	}
	if launch == nil {
		return nil, errors.New("the selected harness is not configured on this runner")
	}
	if harness == sessionpb.Harness_HARNESS_CODEX {
		return nil, nil
	}
	client := c.ModelConfigClient
	if client == nil {
		if c.ModelConfigTarget == nil {
			return nil, errors.New("model configuration service is not configured")
		}
		conn, err := grpc.NewClient(*c.ModelConfigTarget, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithNoProxy(), observability.GRPCClientOption())
		if err != nil {
			return nil, fmt.Errorf("configure model configuration service: %w", err)
		}
		defer conn.Close()
		client = modelconfigpb.NewModelConfigServiceClient(conn)
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	response, err := client.GetModelConfig(ctx, &modelconfigpb.GetModelConfigRequest{Model: model})
	if err != nil {
		return nil, fmt.Errorf("model configuration lookup failed: %w", err)
	}
	if response.TotalContextBudgetTokens == nil {
		return nil, nil
	}
	budget := response.GetTotalContextBudgetTokens()
	if budget <= 0 || int64(int(budget)) != budget {
		return nil, errors.New("model configuration service returned invalid budget")
	}
	result := int(budget)
	return &result, nil
}

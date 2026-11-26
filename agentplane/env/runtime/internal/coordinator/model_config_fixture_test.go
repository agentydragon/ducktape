package coordinator

import (
	"context"
	"sync"

	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

// fixtureModelConfig supplies a budget at the model configuration client boundary.
// Tests can install a hook to observe or block the lookup while exercising admission.
type fixtureModelConfig struct {
	beforeLookup func(context.Context) error
}

func (f fixtureModelConfig) GetModelConfig(ctx context.Context, _ *modelconfigpb.GetModelConfigRequest, _ ...grpc.CallOption) (*modelconfigpb.GetModelConfigResponse, error) {
	if f.beforeLookup != nil {
		if err := f.beforeLookup(ctx); err != nil {
			return nil, err
		}
	}
	return &modelconfigpb.GetModelConfigResponse{TotalContextBudgetTokens: proto.Int64(32768)}, nil
}

func blockModelConfig() (fixtureModelConfig, <-chan struct{}, func()) {
	started := make(chan struct{})
	release := make(chan struct{})
	client := fixtureModelConfig{beforeLookup: func(ctx context.Context) error {
		close(started)
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}}
	return client, started, sync.OnceFunc(func() { close(release) })
}

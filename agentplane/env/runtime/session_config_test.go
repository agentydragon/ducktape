package runtime

import (
	"context"
	"net"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type modelConfigTestServer struct {
	modelconfigpb.UnimplementedModelConfigServiceServer
	lookups atomic.Int32
}

func (s *modelConfigTestServer) GetModelConfig(ctx context.Context, req *modelconfigpb.GetModelConfigRequest) (*modelconfigpb.GetModelConfigResponse, error) {
	s.lookups.Add(1)
	if req.GetModel() != "configured-route" {
		return &modelconfigpb.GetModelConfigResponse{}, nil
	}
	md, _ := metadata.FromIncomingContext(ctx)
	if got := md.Get("authorization"); len(got) != 1 || got[0] != "Bearer test-token" {
		return nil, status.Error(codes.Unauthenticated, "missing bearer")
	}
	budget := int64(32768)
	return &modelconfigpb.GetModelConfigResponse{TotalContextBudgetTokens: &budget}, nil
}

func TestResolveModelConfigUsesGRPCForClaude(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	service := &modelConfigTestServer{}
	modelconfigpb.RegisterModelConfigServiceServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	config := Config{
		Claude: &harness.LaunchConfig{Binary: "claude"}, Codex: &harness.LaunchConfig{Binary: "codex"},
		ModelConfigService: &configuration.Endpoint{Target: listener.Addr().String(), Bearer: "test-token"},
	}
	budget, err := config.ResolveModelConfig(context.Background(), sessionpb.Harness_HARNESS_CODEX, "configured-route")
	if err != nil || budget != nil || service.lookups.Load() != 0 {
		t.Fatalf("Codex budget = %v, err = %v after %d lookups", budget, err, service.lookups.Load())
	}
	budget, err = config.ResolveModelConfig(context.Background(), sessionpb.Harness_HARNESS_CLAUDE, "configured-route")
	if err != nil || budget == nil || *budget != 32768 || service.lookups.Load() != 1 {
		t.Fatalf("Claude budget = %v, err = %v after %d lookups", budget, err, service.lookups.Load())
	}
	budget, err = config.ResolveModelConfig(context.Background(), sessionpb.Harness_HARNESS_CLAUDE, "unknown")
	if err != nil || budget != nil {
		t.Fatalf("unknown model = %v, %v", budget, err)
	}
}

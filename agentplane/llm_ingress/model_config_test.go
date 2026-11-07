package main

import (
	"context"
	"net"
	"testing"

	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestModelConfigGRPC(t *testing.T) {
	resolver, err := auth.NewResolver(api.TokenReviews(), testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := newModelConfigServer(resolver, map[string]modelConfig{"configured": {Model: "configured", TotalContextBudgetTokens: 8192}})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	client := modelconfigpb.NewModelConfigServiceClient(conn)
	request := &modelconfigpb.GetModelConfigRequest{Model: "configured"}
	if _, err := client.GetModelConfig(context.Background(), request); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unauthenticated lookup: %v", err)
	}
	_, wrongAudience := workload(t, "wrong-audience", "another-audience")
	otherContext := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer "+wrongAudience)
	if _, err := client.GetModelConfig(otherContext, request); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("wrong audience lookup: %v", err)
	}
	ctx := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer "+workloadToken(t, "model-reader"))
	response, err := client.GetModelConfig(ctx, request)
	if err != nil || response.TotalContextBudgetTokens == nil || response.GetTotalContextBudgetTokens() != 8192 {
		t.Fatalf("configured lookup: %v, %v", response, err)
	}
	response, err = client.GetModelConfig(ctx, &modelconfigpb.GetModelConfigRequest{Model: "unknown"})
	if err != nil || response.TotalContextBudgetTokens != nil {
		t.Fatalf("unknown lookup: %v, %v", response, err)
	}
}

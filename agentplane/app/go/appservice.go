package main

import (
	"context"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"google.golang.org/protobuf/types/known/emptypb"
)

// appService delegates each group of app-facing RPCs to its owning handler. The HTTP
// gateways mount only the routes for the handler they supply. Watches use the
// gateway's stream encoder directly because its in-process servicer cannot stream.
type appService struct {
	apppb.UnimplementedAppServiceServer
	config   *appConfigService
	threads  *threadMetadataService
	sessions *sessionOpeningService
}

func (s *appService) ListModels(ctx context.Context, req *emptypb.Empty) (*apppb.ListModelsResponse, error) {
	return s.config.ListModels(ctx, req)
}

func (s *appService) ListSandboxPresets(ctx context.Context, req *emptypb.Empty) (*apppb.ListSandboxPresetsResponse, error) {
	return s.config.ListSandboxPresets(ctx, req)
}

func (s *appService) GetThreadMetadata(ctx context.Context, req *apppb.GetThreadMetadataRequest) (*apppb.ThreadMetadata, error) {
	return s.threads.GetThreadMetadata(ctx, req)
}

func (s *appService) ListThreadMetadata(ctx context.Context, req *apppb.ListThreadMetadataRequest) (*apppb.ListThreadMetadataResponse, error) {
	return s.threads.ListThreadMetadata(ctx, req)
}

func (s *appService) OpenSession(ctx context.Context, req *apppb.OpenSessionRequest) (*apppb.OpenSessionResponse, error) {
	return s.sessions.OpenSession(ctx, req)
}

func (s *appService) GetSessionOpening(ctx context.Context, req *apppb.GetSessionOpeningRequest) (*apppb.GetSessionOpeningResponse, error) {
	return s.sessions.GetSessionOpening(ctx, req)
}

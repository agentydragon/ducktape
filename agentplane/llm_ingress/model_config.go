package main

import (
	"context"

	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// modelConfigService reads the same immutable configuration as the model ingress.
type modelConfigService struct {
	modelconfigpb.UnimplementedModelConfigServiceServer
	models map[string]modelConfig
}

func (s *modelConfigService) GetModelConfig(_ context.Context, request *modelconfigpb.GetModelConfigRequest) (*modelconfigpb.GetModelConfigResponse, error) {
	if request.GetModel() == "" {
		return nil, status.Error(codes.InvalidArgument, "model is required")
	}
	response := &modelconfigpb.GetModelConfigResponse{}
	if model, ok := s.models[request.GetModel()]; ok {
		budget := int64(model.TotalContextBudgetTokens)
		response.TotalContextBudgetTokens = &budget
	}
	return response, nil
}

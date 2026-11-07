package main

import (
	"context"
	"net/http"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	gatewayruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func RegisterStatusRoutes(server *HTTPServer, service *appService) error {
	mux := gatewayruntime.NewServeMux()
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), mux, service); err != nil {
		return err
	}
	server.RegisterRawRoute("GET /upstream/app/v1/system/status", http.StripPrefix("/upstream/app", mux), false)
	return nil
}

func (s *appService) GetSystemStatus(ctx context.Context, _ *apppb.GetSystemStatusRequest) (*apppb.GetSystemStatusResponse, error) {
	if s.systemStatus == nil {
		return nil, grpcstatus.Error(codes.Unavailable, "System status checker is not configured")
	}

	components := s.systemStatus.CheckAll(ctx)
	response := &apppb.GetSystemStatusResponse{Components: make([]*apppb.SystemComponentStatus, 0, len(components))}
	for _, component := range components {
		state := apppb.SystemComponentState_SYSTEM_COMPONENT_STATE_DOWN
		if component.State == "ok" {
			state = apppb.SystemComponentState_SYSTEM_COMPONENT_STATE_OK
		}
		response.Components = append(response.Components, &apppb.SystemComponentStatus{
			Name: component.Name, State: state, Detail: component.Detail,
			CheckedAt: timestamppb.New(component.CheckedAt),
		})
	}
	return response, nil
}

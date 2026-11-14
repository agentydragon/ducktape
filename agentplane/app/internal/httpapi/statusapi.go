package httpapi

import (
	"context"
	"net/http"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/internal/status"
	gatewayruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// SystemStatusHandler serves app dependency health through its generated AppService route.
type SystemStatusHandler struct {
	apppb.UnimplementedAppServiceServer
	systemStatus *status.Checker
}

// NewSystemStatusHandler creates a handler backed by the app's dependency health checks.
func NewSystemStatusHandler(systemStatus *status.Checker) *SystemStatusHandler {
	return &SystemStatusHandler{systemStatus: systemStatus}
}

// RegisterRoutes mounts the generated AppService system-status route.
func (h *SystemStatusHandler) RegisterRoutes(server RouteRegistrar) error {
	mux := gatewayruntime.NewServeMux()
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), mux, h); err != nil {
		return err
	}
	server.RegisterRawRoute("GET /upstream/app/v1/system/status", http.StripPrefix("/upstream/app", mux), false)
	return nil
}

func (h *SystemStatusHandler) GetSystemStatus(ctx context.Context, _ *apppb.GetSystemStatusRequest) (*apppb.GetSystemStatusResponse, error) {
	if h.systemStatus == nil {
		return nil, grpcstatus.Error(codes.Unavailable, "System status checker is not configured")
	}

	components := h.systemStatus.CheckAll(ctx)
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

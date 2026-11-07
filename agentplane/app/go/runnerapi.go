package main

import (
	"context"
	"log/slog"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	gatewayruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
)

type runnerStatusReader interface {
	EnvironmentRuntimeStatus(context.Context, string, string) (*sessionpb.EnvironmentRuntimeStatus, error)
}

// RegisterRunnerRoutes exposes the generated AppService HTTP route through the app's
// authenticated caller boundary.
func RegisterRunnerRoutes(server *HTTPServer, service *appService) error {
	mux := gatewayruntime.NewServeMux(gatewayruntime.WithErrorHandler(sessionGatewayError))
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), mux, service); err != nil {
		return err
	}
	server.RegisterRawRoute("GET /sandboxes/{name}/runner-status", mux, false)
	return nil
}

func (s *appService) GetEnvironmentRuntimeStatus(
	ctx context.Context, request *apppb.GetEnvironmentRuntimeStatusRequest,
) (*sessionpb.EnvironmentRuntimeStatus, error) {
	if s.runnerStatus == nil {
		slog.ErrorContext(ctx, "Runner status proxy is not configured")
		return nil, grpcstatus.Error(codes.Unavailable, "Upstream service unavailable")
	}
	if request.GetName() == "" || request.GetSandboxUid() == "" {
		return nil, grpcstatus.Error(codes.InvalidArgument, "Sandbox name and UID are required")
	}
	status, err := s.runnerStatus.EnvironmentRuntimeStatus(ctx, request.GetName(), request.GetSandboxUid())
	if err != nil {
		return nil, sessionRPCError(err)
	}
	return status, nil
}

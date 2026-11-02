package main

import (
	"context"
	"errors"
	"log/slog"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	gatewayruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

type runnerStatusReader interface {
	EnvironmentRuntimeStatus(context.Context, string, string) (*sessionpb.EnvironmentRuntimeStatus, error)
}

// RegisterRunnerRoutes exposes the generated AppService HTTP route through the app's
// authenticated caller boundary. Keep the existing ProtoJSON body for cached browser bundles.
func RegisterRunnerRoutes(server *HTTPServer, service *appService) error {
	mux := gatewayruntime.NewServeMux(
		gatewayruntime.WithErrorHandler(sessionGatewayError),
		gatewayruntime.WithMarshalerOption(gatewayruntime.MIMEWildcard, &gatewayruntime.JSONPb{
			MarshalOptions: protojson.MarshalOptions{UseProtoNames: true, EmitUnpopulated: true},
		}),
	)
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
		return nil, runnerStatusRPCError(err)
	}
	return status, nil
}

// Preserve the old Huma route's generic 503 for runner service/refusal and request-cancellation
// failures. Other Sandbox inventory and upstream errors use the AppService's shared RPC mapping.
func runnerStatusRPCError(err error) error {
	var service *bridge.ServiceError
	var refused *bridge.RefusedError
	if errors.As(err, &service) || errors.As(err, &refused) || errors.Is(err, context.Canceled) {
		slog.Error("upstream request failed", "status", 503, "response", "Upstream service unavailable", "error", err)
		return grpcstatus.Error(codes.Unavailable, "Upstream service unavailable")
	}
	return sessionRPCError(err)
}

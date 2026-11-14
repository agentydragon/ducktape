package server

import (
	"context"
	"fmt"
	"net/http"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/protobuf/types/known/emptypb"
)

// registerThreadMetadataGateway keeps the generated AppService gateway and browser routes at the
// app composition root. Thread behavior is owned by the resource handler behind appService.
func registerThreadMetadataGateway(server *httpServer, service *appService) error {
	mux := runtime.NewServeMux(runtime.WithErrorHandler(sessionGatewayError))
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), mux, service); err != nil {
		return fmt.Errorf("register generated Thread Metadata gateway: %w", err)
	}
	gateway := http.StripPrefix("/upstream/app", mux)
	server.RegisterRawRoute("GET /upstream/app/v1/threads", gateway, false)
	server.RegisterRawRoute("GET /upstream/app/v1/threads/{thread_id}", gateway, false)
	server.RegisterRawRoute("PATCH /upstream/app/v1/threads/{thread_id}", gateway, false)
	server.RegisterRawRoute("POST /upstream/app/v1/threads/{thread_id}/archive", gateway, false)
	server.RegisterRawRoute("POST /upstream/app/v1/threads/{thread_id}/unarchive", gateway, false)
	// grpc-gateway's in-process registration supports unary calls only. Forward the same typed
	// watch through its standard stream encoder, without a local gRPC listener or transport.
	server.RegisterRawRoute("GET /upstream/app/v1/threads:watch", service.threadMetadata.WatchHandler(mux), false)
	return nil
}

func (s *appService) RenameThread(ctx context.Context, request *apppb.RenameThreadRequest) (*apppb.ThreadMetadata, error) {
	return s.threadMetadata.RenameThread(ctx, request)
}

func (s *appService) ArchiveThread(ctx context.Context, request *apppb.ThreadMetadataTarget) (*emptypb.Empty, error) {
	return s.threadMetadata.ArchiveThread(ctx, request)
}

func (s *appService) UnarchiveThread(ctx context.Context, request *apppb.ThreadMetadataTarget) (*emptypb.Empty, error) {
	return s.threadMetadata.UnarchiveThread(ctx, request)
}

func (s *appService) GetThreadMetadata(ctx context.Context, request *apppb.ThreadMetadataTarget) (*apppb.ThreadMetadata, error) {
	return s.threadMetadata.GetThreadMetadata(ctx, request)
}

func (s *appService) ListThreadMetadata(ctx context.Context, request *apppb.ListThreadMetadataRequest) (*apppb.ListThreadMetadataResponse, error) {
	return s.threadMetadata.ListThreadMetadata(ctx, request)
}

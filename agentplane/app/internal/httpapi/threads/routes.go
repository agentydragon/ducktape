package threads

import (
	"context"
	"fmt"
	"net/http"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/internal/httpapi"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
)

// RegisterRoutes mounts Thread metadata reads, mutations and its generated ProtoJSON watch through
// the caller registrar's authenticated boundary.
func (h *Handler) RegisterRoutes(server httpapi.RouteRegistrar) error {
	mux := runtime.NewServeMux(runtime.WithErrorHandler(httpapi.AppServiceGatewayError))
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), mux, h); err != nil {
		return fmt.Errorf("register generated Thread Metadata gateway: %w", err)
	}
	gateway := http.StripPrefix("/upstream/app", mux)
	server.RegisterRawRoute("GET /upstream/app/v1/threads", gateway, false)
	server.RegisterRawRoute("GET /upstream/app/v1/threads/{thread_id}", gateway, false)
	server.RegisterRawRoute("PATCH /upstream/app/v1/threads/{thread_id}", gateway, false)
	server.RegisterRawRoute("POST /upstream/app/v1/threads/{thread_id}/archive", gateway, false)
	server.RegisterRawRoute("POST /upstream/app/v1/threads/{thread_id}/unarchive", gateway, false)
	// These preferences are shared between operator browsers. Workload credentials cannot
	// inspect which notices the operator has read or suppress their display.
	server.RegisterRawRoute("POST /upstream/app/v1/threads/{thread_id}/notice-acknowledgements", gateway, true)
	server.RegisterRawRoute("POST /upstream/app/v1/threads/{thread_id}/notice-acknowledgements:batchGet", gateway, true)
	server.RegisterRawRoute("GET /upstream/app/v1/threads/{thread_id}/notice-acknowledgements", gateway, true)
	// grpc-gateway's in-process registration supports unary calls only. Forward the same typed
	// watch through its standard stream encoder, without a local gRPC listener or transport.
	server.RegisterRawRoute("GET /upstream/app/v1/threads:watch", h.WatchHandler(mux), false)
	return nil
}

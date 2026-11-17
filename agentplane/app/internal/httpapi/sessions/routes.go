package sessions

import (
	"context"
	"fmt"
	"net/http"
	"strings"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/internal/httpapi"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/protobuf/proto"
)

// RegisterRoutes mounts Session creation through the registrar's authenticated boundary.
func (h *Handler) RegisterRoutes(server httpapi.RouteRegistrar) error {
	sessionGateway := runtime.NewServeMux(
		runtime.WithForwardResponseOption(func(ctx context.Context, w http.ResponseWriter, _ proto.Message) error {
			if method, ok := runtime.RPCMethod(ctx); ok && strings.HasSuffix(method, "/CreateSession") {
				w.WriteHeader(http.StatusCreated)
			}
			return nil
		}),
		runtime.WithErrorHandler(httpapi.AppServiceGatewayError),
	)
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), sessionGateway, h); err != nil {
		return fmt.Errorf("register generated Session gateway: %w", err)
	}
	server.RegisterRawRoute("POST /sandboxes/{name}/sessions", sessionGateway, false)
	return nil
}

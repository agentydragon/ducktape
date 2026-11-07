// Package facade serves the generated HTTP/JSON form of the management gRPC API.
package facade

import (
	"context"
	"net/http"
	"time"

	grpcfacade "github.com/agentydragon/ducktape/agentplane/grpc/facade"
	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/v1/managementpb"
)

// New forwards HTTP calls to the management server. As with the underlying facade, automatic
// gRPC retries are disabled because requests may rotate provider credentials.
func New(ctx context.Context, target string, timeout time.Duration) (http.Handler, error) {
	return grpcfacade.NewHandlerWithOptions(ctx, target, timeout,
		grpcfacade.HandlerOptions{RejectUnknownJSONFields: true},
		managementpb.RegisterSessionManagementHandlerFromEndpoint)
}

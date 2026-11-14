package server

import (
	"context"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/protobuf/encoding/protojson"
)

// registerRunnerRoutes exposes the generated AppService HTTP route through the app's
// authenticated caller boundary. Keep the existing ProtoJSON body for cached browser bundles.
func registerRunnerRoutes(server *httpServer, service *appService) error {
	mux := runtime.NewServeMux(
		runtime.WithErrorHandler(sessionGatewayError),
		runtime.WithMarshalerOption(runtime.MIMEWildcard, &runtime.JSONPb{
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
	return s.sessions.GetEnvironmentRuntimeStatus(ctx, request)
}

package gateway

import (
	"context"
	"net/http"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/v1/managementpb"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/protobuf/encoding/protojson"
)

// HTTPHandler provides the generated HTTP API without a loopback server or
// gRPC hop. The caller must authenticate and authorize requests before invoking
// it; the direct generated handler does not run gRPC server interceptors.
func HTTPHandler(ctx context.Context, service *Service) (http.Handler, error) {
	mux := runtime.NewServeMux(runtime.WithMarshalerOption(runtime.MIMEWildcard, &runtime.HTTPBodyMarshaler{Marshaler: &runtime.JSONPb{MarshalOptions: protojson.MarshalOptions{EmitUnpopulated: true}}}))
	if err := managementpb.RegisterSessionManagementHandlerServer(ctx, mux, service); err != nil {
		return nil, err
	}
	return mux, nil
}

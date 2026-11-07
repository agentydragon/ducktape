// Package facade serves the generated HTTP/JSON form of the management gRPC API.
package facade

import (
	"context"
	"errors"
	"net/http"
	"time"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/v1/managementpb"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/encoding/protojson"
)

// New forwards HTTP calls to the loopback management server. Automatic gRPC retries are disabled
// because requests may rotate provider credentials. Unknown JSON fields are rejected.
func New(ctx context.Context, target string, timeout time.Duration) (http.Handler, error) {
	if timeout <= 0 {
		return nil, errors.New("management timeout must be positive")
	}
	mux := runtime.NewServeMux(runtime.WithMarshalerOption(runtime.MIMEWildcard, &runtime.HTTPBodyMarshaler{
		Marshaler: &runtime.JSONPb{MarshalOptions: protojson.MarshalOptions{EmitUnpopulated: true}},
	}))
	opts := []grpc.DialOption{
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDisableRetry(),
		grpc.WithUnaryInterceptor(func(ctx context.Context, method string, request, response any, conn *grpc.ClientConn, invoke grpc.UnaryInvoker, options ...grpc.CallOption) error {
			bounded, cancel := context.WithTimeout(ctx, timeout)
			defer cancel()
			return invoke(bounded, method, request, response, conn, options...)
		}),
	}
	if err := managementpb.RegisterSessionManagementHandlerFromEndpoint(ctx, mux, target, opts); err != nil {
		return nil, err
	}
	return mux, nil
}

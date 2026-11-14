package server

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// The generated unary gateway invokes the app-owned service in process, after RequireCaller
// authenticates HTTP callers and enforces cookie CSRF. Dispatcher remains a native gRPC call.
func registerSessionRoutes(server *httpServer, service *appService) error {
	mux := runtime.NewServeMux(
		runtime.WithForwardResponseOption(func(ctx context.Context, w http.ResponseWriter, _ proto.Message) error {
			if method, ok := runtime.RPCMethod(ctx); ok && strings.HasSuffix(method, "/CreateSession") {
				w.WriteHeader(http.StatusCreated)
			}
			return nil
		}),
		runtime.WithErrorHandler(sessionGatewayError),
	)
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), mux, service); err != nil {
		return err
	}
	server.RegisterRawRoute("POST /sandboxes/{name}/sessions", mux, false)
	return nil
}

func (s *appService) CreateSession(ctx context.Context, request *apppb.CreateSessionRequest) (*apppb.CreateSessionResponse, error) {
	return s.sessions.CreateSession(ctx, request)
}

// Retain HTTP statuses and detail for cached clients while exposing the gRPC status to new clients.
func sessionGatewayError(_ context.Context, _ *runtime.ServeMux, _ runtime.Marshaler, w http.ResponseWriter, _ *http.Request, err error) {
	st := status.Convert(err)
	httpStatus := runtime.HTTPStatusFromCode(st.Code())
	switch st.Code() {
	case codes.InvalidArgument:
		httpStatus = http.StatusUnprocessableEntity
	case codes.FailedPrecondition, codes.AlreadyExists, codes.Aborted:
		httpStatus = http.StatusConflict
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(httpStatus)
	_ = json.NewEncoder(w).Encode(map[string]any{"code": int32(st.Code()), "message": st.Message(), "detail": st.Message()})
}

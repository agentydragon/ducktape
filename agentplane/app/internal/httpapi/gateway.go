package httpapi

import (
	"context"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/app/internal/httpresponse"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// AppServiceGatewayError preserves the browser error contract for generated AppService routes.
func AppServiceGatewayError(_ context.Context, _ *runtime.ServeMux, _ runtime.Marshaler, w http.ResponseWriter, _ *http.Request, err error) {
	st := status.Convert(err)
	httpStatus := runtime.HTTPStatusFromCode(st.Code())
	switch st.Code() {
	case codes.InvalidArgument:
		httpStatus = http.StatusUnprocessableEntity
	case codes.FailedPrecondition, codes.AlreadyExists, codes.Aborted:
		httpStatus = http.StatusConflict
	}
	httpresponse.WriteJSON(w, httpStatus, map[string]any{"code": int32(st.Code()), "message": st.Message(), "detail": st.Message()})
}

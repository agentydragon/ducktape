// Package operator owns the public MCP linkage callback. It consumes the provider's state and code,
// completes the one-shot LinkageService exchange, and returns a redirect or scrubbed plain-text
// error response. The callback remains public because the provider redirects without app cookies;
// LinkageService validates the state capability and owns the authorization flow.
package operator

import (
	"net/http"
	"reflect"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/linkage_servicepb"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type operatorRoutes struct {
	callback linkage_servicepb.LinkageServiceClient
}

type RouteRegistrar interface {
	Handle(string, http.Handler)
}

// RegisterRoutes mounts the public MCP OAuth callback on the supplied app mux.
func RegisterRoutes(server RouteRegistrar, callback linkage_servicepb.LinkageServiceClient) {
	routes := &operatorRoutes{callback: callback}
	server.Handle("GET /mcp-linkage/callback", http.HandlerFunc(routes.completeMCPLinkage))
}

func (routes *operatorRoutes) completeMCPLinkage(w http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	if !query.Has("state") || !query.Has("code") {
		http.Error(w, "MCP linkage callback failed", http.StatusBadRequest)
		return
	}
	if nilDependency(routes.callback) {
		http.Error(w, "MCP linkage callback failed", http.StatusServiceUnavailable)
		return
	}
	// Preserve the old proxy's request-context-only lifetime. The Action Service's token client
	// bounds the exchange, and a disconnect cancels it; never retry this one-shot state capability.
	_, err := routes.callback.CompleteCallback(request.Context(), &linkage_servicepb.CompleteCallbackRequest{
		State: query.Get("state"), Code: query.Get("code"),
	})
	if err != nil {
		responseStatus := http.StatusServiceUnavailable
		switch status.Code(err) {
		case codes.InvalidArgument:
			responseStatus = http.StatusBadRequest
		case codes.FailedPrecondition:
			responseStatus = http.StatusConflict
		case codes.NotFound:
			responseStatus = http.StatusNotFound
		case codes.Unavailable:
			if isTokenExchangeFailure(err) {
				responseStatus = http.StatusBadGateway
			}
		case codes.Canceled:
			if request.Context().Err() != nil {
				return
			}
		}
		// Provider diagnostics and state/code capabilities never belong in the public response.
		http.Error(w, "MCP linkage callback failed", responseStatus)
		return
	}
	http.Redirect(w, request, "/#/mcp-servers?linked=1", http.StatusSeeOther)
}

func isTokenExchangeFailure(err error) bool {
	for _, detail := range status.Convert(err).Details() {
		if info, ok := detail.(*errdetails.ErrorInfo); ok && info.Domain == "agentplane.actions" && info.Reason == "MCP_TOKEN_EXCHANGE_FAILED" {
			return true
		}
	}
	return false
}

func nilDependency(dependency any) bool {
	if dependency == nil {
		return true
	}
	value := reflect.ValueOf(dependency)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}

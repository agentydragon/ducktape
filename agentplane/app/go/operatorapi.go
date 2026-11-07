package main

import (
	"encoding/json"
	"net/http"
	"reflect"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type operatorRoutes struct {
	callback linkage_servicepb.LinkageServiceClient
}

// RegisterOperatorRoutes installs the public MCP OAuth callback bridge. The callback is handled
// here because it is a third-party redirect endpoint; the app carries it to Action Service over gRPC.
func RegisterOperatorRoutes(
	server *HTTPServer,
	callback linkage_servicepb.LinkageServiceClient,
) {
	routes := &operatorRoutes{callback: callback}
	// Keep the public OAuth callback as a raw route: Huma's normal JSON response machinery
	// interprets Location/body fields as an API response and cannot preserve the redirect status
	// plus HTML body contract used by the browser callback.
	server.mux.Handle("GET /mcp-linkage/callback", http.HandlerFunc(routes.completeMCPLinkage))
}

func (routes *operatorRoutes) completeMCPLinkage(w http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	missing := make([]string, 0, 2)
	for _, name := range []string{"state", "code"} {
		if _, present := query[name]; !present {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		validationErrors := make([]map[string]any, 0, len(missing))
		for _, name := range missing {
			validationErrors = append(validationErrors, map[string]any{
				"loc": []string{"query", name}, "msg": "Field required", "type": "missing",
			})
		}
		body, _ := json.Marshal(map[string]any{"detail": validationErrors})
		writeCallbackBody(w, http.StatusUnprocessableEntity, "application/json", "", body)
		return
	}
	if nilDependency(routes.callback) {
		writeCallbackDetail(w, http.StatusServiceUnavailable, "Action LinkageService is not configured")
		return
	}
	// Preserve the old proxy's request-context-only lifetime. The Action Service's token client
	// bounds the exchange, and a disconnect cancels it; never retry this one-shot state capability.
	_, err := routes.callback.CompleteCallback(request.Context(), &linkage_servicepb.CompleteCallbackRequest{
		State: query.Get("state"), Code: query.Get("code"),
	})
	if err != nil {
		switch status.Code(err) {
		case codes.InvalidArgument:
			writeCallbackDetail(w, http.StatusBadRequest, "MCP linkage callback failed")
		case codes.FailedPrecondition:
			writeCallbackDetail(w, http.StatusConflict, "MCP linkage callback failed")
		case codes.NotFound:
			writeCallbackDetail(w, http.StatusNotFound, "MCP linkage callback failed")
		case codes.Unavailable:
			if isTokenExchangeFailure(err) {
				// The old HTTP callback returned 502 for token-exchange failures. Do not forward the
				// provider's error description or any callback capability in the public response.
				writeCallbackDetail(w, http.StatusBadGateway, "MCP linkage callback failed")
			} else {
				writeCallbackDetail(w, http.StatusServiceUnavailable, "MCP linkage callback failed")
			}
		case codes.DeadlineExceeded:
			writeCallbackTransportFailure(w, codes.DeadlineExceeded.String())
		case codes.Canceled:
			if request.Context().Err() != nil {
				return
			}
			writeCallbackTransportFailure(w, codes.Canceled.String())
		default:
			writeCallbackDetail(w, http.StatusServiceUnavailable, "MCP linkage callback failed")
		}
		return
	}
	const location = "/#/mcp-servers?linked=1"
	body := []byte(`<html><head><title>303 See Other</title></head><body><a href="/#/mcp-servers?linked=1">See Other</a></body></html>`)
	writeCallbackBody(w, http.StatusSeeOther, "text/html; charset=utf-8", location, body)
}

func writeCallbackTransportFailure(w http.ResponseWriter, errorType string) {
	body, _ := json.Marshal(map[string]any{"detail": map[string]any{
		"service": "agentplane.action_service.LinkageService", "method": "CompleteCallback", "error_type": errorType,
	}})
	writeCallbackBody(w, http.StatusServiceUnavailable, "application/json", "", body)
}

func isTokenExchangeFailure(err error) bool {
	for _, detail := range status.Convert(err).Details() {
		if info, ok := detail.(*errdetails.ErrorInfo); ok && info.Domain == "agentplane.actions" && info.Reason == "MCP_TOKEN_EXCHANGE_FAILED" {
			return true
		}
	}
	return false
}

func writeCallbackDetail(w http.ResponseWriter, status int, detail string) {
	body, _ := json.Marshal(map[string]string{"detail": detail})
	writeCallbackBody(w, status, "application/json", "", body)
}

func writeCallbackBody(w http.ResponseWriter, status int, contentType, location string, body []byte) {
	w.Header().Set("Content-Type", contentType)
	if location != "" {
		w.Header().Set("Location", location)
	}
	w.WriteHeader(status)
	_, _ = w.Write(body)
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

package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"reflect"
	"strings"
)

type callbackUpstream interface {
	OpenRequest(context.Context, string, string, string, string, http.Header, io.Reader) (*http.Response, error)
}

type operatorRoutes struct {
	callback         callbackUpstream
	actionServiceURL string
}

// RegisterOperatorRoutes installs the public MCP OAuth callback bridge. The callback is handled
// here because it is a third-party redirect endpoint without a generated Action Service RPC.
func RegisterOperatorRoutes(
	server *HTTPServer,
	callback callbackUpstream,
	actionServiceURL string,
) {
	routes := &operatorRoutes{
		callback: callback, actionServiceURL: actionServiceURL,
	}
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
		writeCallbackDetail(w, http.StatusServiceUnavailable, "Action facade is not configured")
		return
	}
	upstream, err := routes.callback.OpenRequest(request.Context(), http.MethodGet, "v1/mcp-linkage/callback",
		(url.Values{"state": {request.URL.Query().Get("state")}, "code": {request.URL.Query().Get("code")}}).Encode(), "",
		http.Header{"Accept": []string{"application/json"}}, nil)
	if err != nil {
		body, _ := json.Marshal(map[string]any{"detail": map[string]any{
			"method": http.MethodGet, "url": routes.callbackURL(), "upstream_status": nil,
			"error_type": safeOperatorErrorType(err),
		}})
		writeCallbackBody(w, http.StatusServiceUnavailable, "application/json", "", body)
		return
	}
	if upstream == nil {
		writeCallbackDetail(w, http.StatusBadGateway, "Action callback returned no response")
		return
	}
	if upstream.Body != nil {
		defer upstream.Body.Close()
	}
	if upstream.StatusCode >= 300 && upstream.StatusCode < 400 {
		writeCallbackDetail(w, http.StatusBadGateway, "Action callback redirects are not accepted")
		return
	}
	if upstream.StatusCode < 200 || upstream.StatusCode >= 300 {
		writeCallbackDetail(w, upstream.StatusCode, "MCP linkage callback failed")
		return
	}
	const location = "/#/mcp-servers?linked=1"
	body := []byte(`<html><head><title>303 See Other</title></head><body><a href="/#/mcp-servers?linked=1">See Other</a></body></html>`)
	writeCallbackBody(w, http.StatusSeeOther, "text/html; charset=utf-8", location, body)
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

func (routes *operatorRoutes) callbackURL() string {
	return sanitizedActionURL(routes.actionServiceURL, "/v1/mcp-linkage/callback")
}

func sanitizedActionURL(raw, path string) string {
	parsed, err := url.Parse(raw)
	if err != nil || !parsed.IsAbs() || parsed.Host == "" {
		return path
	}
	parsed.User = nil
	parsed.Path = path
	parsed.RawPath = ""
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String()
}

func safeOperatorErrorType(err error) string {
	var networkError net.Error
	if errors.As(err, &networkError) {
		return "TransportError"
	}
	kind := reflect.TypeOf(err)
	for kind != nil && kind.Kind() == reflect.Pointer {
		kind = kind.Elem()
	}
	if kind == nil {
		return "RequestError"
	}
	name := kind.Name()
	if strings.Contains(name, "Error") {
		return name
	}
	return "RequestError"
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

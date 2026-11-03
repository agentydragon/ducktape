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
	"sync"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/policyaccess"
	"github.com/danielgtaylor/huma/v2"
)

type callbackUpstream interface {
	OpenRequest(context.Context, string, string, string, string, http.Header, io.Reader) (*http.Response, error)
}

type operatorRoutes struct {
	policies         *policyaccess.Access
	federator        policyaccess.ActionFederator
	actions          policyaccess.ActionPolicyRPC
	callback         callbackUpstream
	actionServiceURL string
}

type actionPolicySetsOutput struct {
	Body []policyaccess.ActionPolicySetView
}

type callerGrantsInput struct {
	Namespace string `path:"namespace" minLength:"1"`
	Name      string `path:"name" minLength:"1"`
}

type callerGrantsOutput struct {
	Body callerGrantsView
}

type callerGrantsView struct {
	Bindings     []policyaccess.BindingView `json:"bindings"`
	ActionPolicy any                        `json:"action_policy"`
}

// RegisterOperatorRoutes installs the caller-readable Action policy inventory, operator-only
// account grants view, and public MCP OAuth callback bridge. The callback is handled here because
// it is a third-party redirect endpoint without a generated Action Service RPC.
func RegisterOperatorRoutes(
	server *HTTPServer,
	policies *policyaccess.Access,
	federator policyaccess.ActionFederator,
	actions policyaccess.ActionPolicyRPC,
	callback callbackUpstream,
	actionServiceURL string,
) {
	routes := &operatorRoutes{
		policies: policies, federator: federator, actions: actions,
		callback: callback, actionServiceURL: actionServiceURL,
	}
	api := server.ProtectedAPI()
	huma.Register[struct{}, actionPolicySetsOutput](api, huma.Operation{
		OperationID: "listActionPolicySets", Method: http.MethodGet, Path: "/action-policy/sets", Tags: []string{"action-policy"},
		Summary: "List Action PolicySets and their parser verdicts",
	}, routes.listActionPolicySets)
	huma.Register[callerGrantsInput, callerGrantsOutput](api, huma.Operation{
		OperationID: "getCallerGrants", Method: http.MethodGet, Path: "/caller-grants/{namespace}/{name}", Tags: []string{"operator"},
		Summary: "Inspect policy bindings and effective Action policy for a ServiceAccount",
	}, routes.callerGrants)
	// Keep the public OAuth callback as a raw route: Huma's normal JSON response machinery
	// interprets Location/body fields as an API response and cannot preserve the redirect status
	// plus HTML body contract used by the browser callback.
	server.mux.Handle("GET /mcp-linkage/callback", http.HandlerFunc(routes.completeMCPLinkage))
}

func (routes *operatorRoutes) listActionPolicySets(ctx context.Context, _ *struct{}) (*actionPolicySetsOutput, error) {
	if routes.policies == nil {
		return nil, huma.Error503ServiceUnavailable("Policy access is unavailable")
	}
	views, err := routes.policies.ActionPolicySets(ctx)
	if err != nil {
		return nil, huma.Error503ServiceUnavailable("Action policy inventory is unavailable")
	}
	return &actionPolicySetsOutput{Body: views}, nil
}

func (routes *operatorRoutes) callerGrants(ctx context.Context, input *callerGrantsInput) (*callerGrantsOutput, error) {
	caller, ok := auth.CallerFromContext(ctx)
	if !ok || caller.Kind != auth.CallerOperator {
		return nil, huma.Error403Forbidden("Operator session required")
	}
	session, _ := auth.SessionFromContext(ctx)
	view, err := routes.readCallerGrants(ctx, caller, session, input.Namespace, input.Name)
	if err != nil {
		return nil, err
	}
	return &callerGrantsOutput{Body: *view}, nil
}

func (routes *operatorRoutes) readCallerGrants(
	ctx context.Context,
	caller auth.Caller,
	session *auth.RequestSession,
	namespace string,
	name string,
) (*callerGrantsView, error) {
	if caller.Kind != auth.CallerOperator {
		return nil, huma.Error403Forbidden("Operator session required")
	}
	if namespace == "" || name == "" {
		return nil, huma.Error422UnprocessableEntity("subject must set both namespace and name")
	}
	if routes.policies == nil {
		return nil, huma.Error503ServiceUnavailable("Policy access is unavailable")
	}
	subject := &identitypb.ServiceAccount{Namespace: namespace, Name: name}
	var bindings []policyaccess.BindingView
	var actionPolicy any
	var bindingsErr error
	var group sync.WaitGroup
	group.Add(2)
	go func() {
		defer group.Done()
		bindings, bindingsErr = routes.policies.BindingsFor(ctx, subject)
	}()
	go func() {
		defer group.Done()
		actionPolicy = routes.actionPolicyFrame(ctx, caller, session, subject)
	}()
	group.Wait()
	if bindingsErr != nil {
		return nil, huma.Error503ServiceUnavailable("Policy access is unavailable")
	}
	if bindings == nil {
		bindings = []policyaccess.BindingView{}
	}
	return &callerGrantsView{Bindings: bindings, ActionPolicy: actionPolicy}, nil
}

func (routes *operatorRoutes) actionPolicyFrame(
	ctx context.Context,
	caller auth.Caller,
	session *auth.RequestSession,
	subject *identitypb.ServiceAccount,
) any {
	reader, err := policyaccess.NewActionPolicyReader(routes.federator, routes.actions, routes.actionServiceURL)
	if err != nil {
		return actionPolicyUnavailable("action_facade_not_configured", nil)
	}
	view, unavailable, err := reader.ForSubject(ctx, caller, session, subject)
	if unavailable != nil {
		return unavailable
	}
	if err != nil {
		return actionPolicyUnavailable("upstream_request_failed", &policyaccess.ActionUpstreamFailure{
			Method: http.MethodGet, URL: routes.actionPolicyURL(), UpstreamStatus: intPointer(http.StatusBadGateway), ErrorType: "GRPCResponseError",
		})
	}
	return view
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

func (routes *operatorRoutes) actionPolicyURL() string {
	return sanitizedActionURL(routes.actionServiceURL, "/v1/action-policy")
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

func actionPolicyUnavailable(code string, upstream *policyaccess.ActionUpstreamFailure) *policyaccess.ActionPolicyUnavailable {
	return &policyaccess.ActionPolicyUnavailable{Kind: "unavailable", Code: code, Upstream: upstream}
}

func intPointer(value int) *int { return &value }

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

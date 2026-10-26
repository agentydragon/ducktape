package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/federation"
	"github.com/agentydragon/ducktape/agentplane/app/go/policyaccess"
	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/structpb"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type operatorAPIFederator struct {
	token string
	err   error
	row   federation.SessionLoginRow
	calls int
}

func (f *operatorAPIFederator) Exchange(_ context.Context, row federation.SessionLoginRow) (string, error) {
	f.row = row
	f.calls++
	return f.token, f.err
}

type operatorAPIActionClient struct {
	response *action_servicepb.GetActionPolicyResponse
	err      error
	request  *action_servicepb.GetActionPolicyRequest
	bearer   []string
}

func (c *operatorAPIActionClient) GetActionPolicy(
	ctx context.Context,
	request *action_servicepb.GetActionPolicyRequest,
	_ ...grpc.CallOption,
) (*action_servicepb.GetActionPolicyResponse, error) {
	c.request = request
	md, _ := metadata.FromOutgoingContext(ctx)
	c.bearer = md.Get("authorization")
	return c.response, c.err
}

type operatorAPICallback struct {
	response *http.Response
	err      error
	method   string
	path     string
	query    string
	bearer   string
	headers  http.Header
}

func (c *operatorAPICallback) OpenRequest(
	_ context.Context,
	method, path, query, bearer string,
	headers http.Header,
	_ io.Reader,
) (*http.Response, error) {
	c.method, c.path, c.query, c.bearer, c.headers = method, path, query, bearer, headers.Clone()
	return c.response, c.err
}

type operatorAPICloseTracker struct{ closed bool }

func (c *operatorAPICloseTracker) Read([]byte) (int, error) { return 0, io.EOF }
func (c *operatorAPICloseTracker) Close() error             { c.closed = true; return nil }

func newOperatorPolicyAccess(t *testing.T, objects ...client.Object) *policyaccess.Access {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{policyapi.AddToScheme, egressapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	objectsClient := clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
	return policyaccess.New("policy", objectsClient)
}

func TestCallerGrantsUsesFederatedGeneratedRPCAndReturnsSafeView(t *testing.T) {
	subject := &sandboxespb.ServiceAccount{Namespace: "team-a", Name: "worker"}
	access := newOperatorPolicyAccess(t, &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: "policy", Name: "binding-a", Generation: 2},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.ServiceAccountRef{Namespace: subject.GetNamespace(), Name: subject.GetName()},
			PolicySets: []string{"read-only"},
		},
	})
	schema, err := structpb.NewStruct(map[string]any{"type": "object", "required": []any{"name"}})
	if err != nil {
		t.Fatal(err)
	}
	policy := &action_servicepb.CallerActionPolicy{
		Synced:         true,
		Bindings:       []*action_servicepb.CallerBinding{{Name: "binding-a", Labels: map[string]string{"private": "diagnostic"}}},
		AutoApprovable: []*action_servicepb.ActionIdentity{{Group: "test", Name: "private-diagnostic"}},
		AutoApproveIf: []*action_servicepb.EffectivePolicy{{
			Binding: "binding-a", PolicySet: "read-only", Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
			Policy: &action_servicepb.Policy{Kind: &action_servicepb.Policy_ArgumentSchema{ArgumentSchema: &action_servicepb.ArgumentSchemaPolicy{
				Actions: map[string]*action_servicepb.StringList{"test": {Values: []string{"read"}}}, ArgumentSchema: schema,
			}}},
		}},
	}
	row := &auth.SessionRow{}
	session := &auth.RequestSession{Login: &auth.OperatorSession{}, Row: row}
	federator := &operatorAPIFederator{token: "request-bound-action-token"}
	actions := &operatorAPIActionClient{response: &action_servicepb.GetActionPolicyResponse{ActionPolicy: policy}}
	routes := &operatorRoutes{
		policies: access, federator: federator, actions: actions,
		actionServiceURL: "https://user:secret@actions.example.test/base?credential=hidden#fragment",
	}

	view, err := routes.readCallerGrants(t.Context(), auth.Caller{Kind: auth.CallerOperator}, session, subject.GetNamespace(), subject.GetName())
	if err != nil {
		t.Fatal(err)
	}
	if federator.calls != 1 || federator.row != row {
		t.Fatalf("federation calls/row = %d/%p, want 1/%p", federator.calls, federator.row, row)
	}
	if !reflect.DeepEqual(actions.bearer, []string{"Bearer request-bound-action-token"}) {
		t.Fatalf("outgoing authorization metadata = %v", actions.bearer)
	}
	if actions.request == nil || actions.request.GetSubject().GetNamespace() != subject.GetNamespace() ||
		actions.request.GetSubject().GetName() != subject.GetName() {
		t.Fatalf("GetActionPolicy request = %#v", actions.request)
	}
	if len(view.Bindings) != 1 || view.Bindings[0].Name != "binding-a" {
		t.Fatalf("bindings = %#v", view.Bindings)
	}
	actionView, ok := view.ActionPolicy.(*policyaccess.ActionPolicyView)
	if !ok || !actionView.Synced || len(actionView.AutoApproveIf) != 1 ||
		actionView.AutoApproveIf[0].Policy.Type != "argument_schema" ||
		actionView.AutoApproveIf[0].Policy.ArgumentSchema["required"].([]any)[0] != "name" {
		t.Fatalf("action policy = %#v", view.ActionPolicy)
	}
	encoded, err := json.Marshal(view)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "private-diagnostic") || strings.Contains(string(encoded), "diagnostic") ||
		strings.Contains(string(encoded), "auto_approvable") || !strings.Contains(string(encoded), `"action_policy"`) {
		t.Fatalf("caller-grants leaked generated diagnostics or has wrong shape: %s", encoded)
	}
}

func TestCallerGrantsOperatorGuardAndUnavailableActionFrame(t *testing.T) {
	routes := &operatorRoutes{policies: newOperatorPolicyAccess(t)}
	_, err := routes.readCallerGrants(t.Context(), auth.Caller{Kind: auth.CallerToken}, nil, "team", "worker")
	var statusErr interface{ GetStatus() int }
	if !errors.As(err, &statusErr) || statusErr.GetStatus() != http.StatusForbidden {
		t.Fatalf("non-operator error = %v", err)
	}

	frame := routes.actionPolicyFrame(t.Context(), auth.Caller{Kind: auth.CallerOperator}, nil,
		&sandboxespb.ServiceAccount{Namespace: "team", Name: "worker"})
	unavailable, ok := frame.(*policyaccess.ActionPolicyUnavailable)
	if !ok || unavailable.Code != "operator_federation_not_configured" {
		t.Fatalf("missing federation frame = %#v", frame)
	}

	federator := &operatorAPIFederator{err: &federation.FederationError{Code: "operator_federation_not_configured", Status: http.StatusServiceUnavailable}}
	routes.federator = federator
	frame = routes.actionPolicyFrame(t.Context(), auth.Caller{Kind: auth.CallerOperator},
		&auth.RequestSession{Login: &auth.OperatorSession{}, Row: &auth.SessionRow{}},
		&sandboxespb.ServiceAccount{Namespace: "team", Name: "worker"})
	unavailable, ok = frame.(*policyaccess.ActionPolicyUnavailable)
	if !ok || unavailable.Code != "operator_federation_not_configured" {
		t.Fatalf("federation refusal frame = %#v", frame)
	}
}

func TestCallerGrantsMapsActionRPCFailuresWithoutLeakingConfiguredURL(t *testing.T) {
	federator := &operatorAPIFederator{token: "federated"}
	actions := &operatorAPIActionClient{err: status.Error(codes.PermissionDenied, "sensitive provider detail")}
	routes := &operatorRoutes{
		federator: federator, actions: actions,
		actionServiceURL: "https://user:secret@actions.example.test/base?secret=hidden#fragment",
	}
	frame := routes.actionPolicyFrame(t.Context(), auth.Caller{Kind: auth.CallerOperator},
		&auth.RequestSession{Login: &auth.OperatorSession{}, Row: &auth.SessionRow{}},
		&sandboxespb.ServiceAccount{Namespace: "team", Name: "worker"})
	unavailable, ok := frame.(*policyaccess.ActionPolicyUnavailable)
	if !ok || unavailable.Code != "upstream_request_failed" || unavailable.Upstream == nil ||
		unavailable.Upstream.UpstreamStatus == nil || *unavailable.Upstream.UpstreamStatus != http.StatusForbidden ||
		unavailable.Upstream.URL != "https://actions.example.test/v1/action-policy" ||
		unavailable.Upstream.ErrorType != "HTTPStatusError" {
		t.Fatalf("RPC failure frame = %#v", frame)
	}
}

func TestPublicMCPCallbackForwardsQueryAndRedirects(t *testing.T) {
	body := &operatorAPICloseTracker{}
	callback := &operatorAPICallback{response: &http.Response{StatusCode: http.StatusOK, Body: body}}
	server := NewHTTPServer(nil)
	RegisterOperatorRoutes(server, nil, nil, nil, callback, "https://actions.example.test")
	values := url.Values{"state": {"state +&"}, "code": {"code/with?chars"}}
	request := httptest.NewRequest(http.MethodGet, "/mcp-linkage/callback?"+values.Encode(), nil)
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusSeeOther || recorder.Header().Get("Location") != "/#/mcp-servers?linked=1" {
		t.Fatalf("callback response = %d, Location %q, body %s", recorder.Code, recorder.Header().Get("Location"), recorder.Body.String())
	}
	if callback.method != http.MethodGet || callback.path != "v1/mcp-linkage/callback" ||
		callback.bearer != "" || callback.headers.Get("Accept") != "application/json" || !body.closed {
		t.Fatalf("callback upstream call = method %q path %q bearer %q headers %v closed %v",
			callback.method, callback.path, callback.bearer, callback.headers, body.closed)
	}
	want := url.Values{"state": {"state +&"}, "code": {"code/with?chars"}}
	if callback.query != want.Encode() {
		t.Fatalf("callback query = %q, want %q", callback.query, want.Encode())
	}
}

func TestPublicMCPCallbackRequiresStateAndCode(t *testing.T) {
	for _, test := range []struct {
		query string
		want  []string
	}{
		{query: "", want: []string{"state", "code"}},
		{query: "state=one", want: []string{"code"}},
		{query: "code=two", want: []string{"state"}},
	} {
		server := NewHTTPServer(nil)
		RegisterOperatorRoutes(server, nil, nil, nil, nil, "")
		recorder := httptest.NewRecorder()
		server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/mcp-linkage/callback?"+test.query, nil))
		var response struct {
			Detail []struct {
				Loc  []string `json:"loc"`
				Msg  string   `json:"msg"`
				Type string   `json:"type"`
			} `json:"detail"`
		}
		if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if recorder.Code != http.StatusUnprocessableEntity || len(response.Detail) != len(test.want) {
			t.Fatalf("missing callback parameters response = %d %#v", recorder.Code, response)
		}
		for i, name := range test.want {
			if len(response.Detail[i].Loc) != 2 || response.Detail[i].Loc[0] != "query" || response.Detail[i].Loc[1] != name ||
				response.Detail[i].Msg != "Field required" || response.Detail[i].Type != "missing" {
				t.Fatalf("missing callback parameter %q error = %#v", name, response.Detail[i])
			}
		}
	}
}

func TestPublicMCPCallbackPreservesPythonFailureStatuses(t *testing.T) {
	for _, test := range []struct {
		name       string
		status     int
		wantStatus int
		wantDetail string
	}{
		{name: "redirect", status: http.StatusFound, wantStatus: http.StatusBadGateway, wantDetail: "Action callback redirects are not accepted"},
		{name: "callback refusal", status: http.StatusConflict, wantStatus: http.StatusConflict, wantDetail: "MCP linkage callback failed"},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := &operatorAPICloseTracker{}
			callback := &operatorAPICallback{response: &http.Response{StatusCode: test.status, Body: body}}
			server := NewHTTPServer(nil)
			RegisterOperatorRoutes(server, nil, nil, nil, callback, "https://actions.example.test")
			recorder := httptest.NewRecorder()
			server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
				"/mcp-linkage/callback?state=one&code=two", nil))
			var response map[string]string
			if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if recorder.Code != test.wantStatus || response["detail"] != test.wantDetail || !body.closed {
				t.Fatalf("callback response = %d %v closed=%v", recorder.Code, response, body.closed)
			}
		})
	}
}

func TestPublicMCPCallbackReportsConfigurationAndTransportFailureSafely(t *testing.T) {
	t.Run("unconfigured", func(t *testing.T) {
		server := NewHTTPServer(nil)
		RegisterOperatorRoutes(server, nil, nil, nil, nil, "https://actions.example.test")
		recorder := httptest.NewRecorder()
		server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
			"/mcp-linkage/callback?state=one&code=two", nil))
		var response map[string]string
		if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if recorder.Code != http.StatusServiceUnavailable || response["detail"] != "Action facade is not configured" {
			t.Fatalf("unconfigured callback = %d %v", recorder.Code, response)
		}
	})

	t.Run("transport error is credential free", func(t *testing.T) {
		callback := &operatorAPICallback{err: errors.New("dial failed")}
		server := NewHTTPServer(nil)
		RegisterOperatorRoutes(server, nil, nil, nil, callback, "https://user:secret@actions.example.test/base?token=hidden")
		recorder := httptest.NewRecorder()
		server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
			"/mcp-linkage/callback?state=one&code=two", nil))
		var response struct {
			Detail struct {
				Method         string `json:"method"`
				URL            string `json:"url"`
				UpstreamStatus *int   `json:"upstream_status"`
				ErrorType      string `json:"error_type"`
			} `json:"detail"`
		}
		if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if recorder.Code != http.StatusServiceUnavailable || response.Detail.Method != http.MethodGet ||
			response.Detail.URL != "https://actions.example.test/v1/mcp-linkage/callback" ||
			response.Detail.UpstreamStatus != nil || response.Detail.ErrorType != "RequestError" {
			t.Fatalf("transport callback detail = %d %#v", recorder.Code, response.Detail)
		}
	})
}

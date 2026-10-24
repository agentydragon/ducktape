package policyaccess

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/federation"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type fakeActionFederator struct {
	token string
	err   error
	calls int
}

func (f *fakeActionFederator) Exchange(_ context.Context, _ federation.SessionLoginRow) (string, error) {
	f.calls++
	return f.token, f.err
}

type fakeActionProxy struct {
	response *http.Response
	err      error
	method   string
	path     string
	query    string
	bearer   string
	headers  http.Header
}

func (f *fakeActionProxy) OpenRequest(
	_ context.Context,
	method, path, query, bearer string,
	headers http.Header,
	_ io.Reader,
) (*http.Response, error) {
	f.method, f.path, f.query, f.bearer, f.headers = method, path, query, bearer, headers.Clone()
	return f.response, f.err
}

func actionResponse(t *testing.T, policy *action_servicepb.CallerActionPolicy) *http.Response {
	t.Helper()
	body, err := protojson.Marshal(&action_servicepb.GetActionPolicyResponse{ActionPolicy: policy})
	if err != nil {
		t.Fatal(err)
	}
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(string(body)))}
}

func actionSession() *auth.RequestSession {
	return &auth.RequestSession{Login: &auth.OperatorSession{}, Row: &auth.SessionRow{}}
}

func TestActionPolicyReaderProjectsEveryPolicyKindAndExactRequest(t *testing.T) {
	schema, err := structpb.NewStruct(map[string]any{"type": "object", "required": []any{"name"}})
	if err != nil {
		t.Fatal(err)
	}
	policy := &action_servicepb.CallerActionPolicy{
		Synced: true,
		AutoApproveIf: []*action_servicepb.EffectivePolicy{
			{
				Binding: "grant", PolicySet: "exact", Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES, Index: 0,
				Policy: &action_servicepb.Policy{Kind: &action_servicepb.Policy_ExactActions{ExactActions: &action_servicepb.ExactActionsPolicy{
					Actions: map[string]*action_servicepb.StringList{"test_group": {Values: []string{"record"}}},
				}}},
			},
			{Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES, Policy: &action_servicepb.Policy{Kind: &action_servicepb.Policy_ArgumentSchema{ArgumentSchema: &action_servicepb.ArgumentSchemaPolicy{
				Actions: map[string]*action_servicepb.StringList{}, ArgumentSchema: schema,
			}}}},
			{Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES, Policy: &action_servicepb.Policy{Kind: &action_servicepb.Policy_GithubRepository{GithubRepository: &action_servicepb.GitHubRepositoryPolicy{
				Actions: map[string]*action_servicepb.StringList{}, Owner: "octo", Repository: "repo",
			}}}},
			{Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES, Policy: &action_servicepb.Policy{Kind: &action_servicepb.Policy_GithubPublicRepository{GithubPublicRepository: &action_servicepb.GitHubPublicRepositoryPolicy{
				Actions: map[string]*action_servicepb.StringList{},
			}}}},
			{Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES, Policy: &action_servicepb.Policy{Kind: &action_servicepb.Policy_HomeAssistantEntityControl{HomeAssistantEntityControl: &action_servicepb.HomeAssistantEntityControlPolicy{
				Actions:  map[string]*action_servicepb.StringList{},
				Entities: map[string]*action_servicepb.StringList{"light.kitchen": {Values: []string{"turn_on"}}},
			}}}},
		},
	}
	federator := &fakeActionFederator{token: "short-lived-target-token"}
	upstream := &fakeActionProxy{response: actionResponse(t, policy)}
	reader, err := NewActionPolicyReader(federator, upstream, "https://actions.example.test/?api_key=must-not-leak#fragment")
	if err != nil {
		t.Fatal(err)
	}
	view, unavailable, err := reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(), &sandboxespb.ServiceAccount{
		Namespace: "team-a", Name: "worker",
	})
	if err != nil || unavailable != nil {
		t.Fatalf("ForSubject() = %#v, %#v, %v", view, unavailable, err)
	}
	if !view.Synced || len(view.AutoApproveIf) != 5 || view.AutoApproveIf[0].Source != "rules" ||
		view.AutoApproveIf[0].Policy.Type != "exact_actions" || view.AutoApproveIf[0].Policy.Actions["test_group"][0] != "record" ||
		view.AutoApproveIf[1].Policy.ArgumentSchema["type"] != "object" ||
		view.AutoApproveIf[2].Policy.Owner != "octo" || view.AutoApproveIf[2].Policy.Repository != "repo" ||
		view.AutoApproveIf[3].Policy.Type != "github_public_repository" ||
		view.AutoApproveIf[4].Policy.Entities["light.kitchen"][0] != "turn_on" {
		t.Fatalf("effective policy projection = %#v", view)
	}
	if federator.calls != 1 || upstream.method != http.MethodGet || upstream.path != "v1/action-policy" ||
		upstream.query != "subject.namespace=team-a&subject.name=worker" || upstream.bearer != "short-lived-target-token" ||
		upstream.headers.Get("Accept") != "application/json" {
		t.Fatalf("federated request: calls=%d method=%q path=%q query=%q bearer=%q headers=%v",
			federator.calls, upstream.method, upstream.path, upstream.query, upstream.bearer, upstream.headers)
	}
	encoded, err := json.Marshal(view)
	if err != nil {
		t.Fatal(err)
	}
	var document map[string]any
	if err := json.Unmarshal(encoded, &document); err != nil {
		t.Fatal(err)
	}
	policies := document["auto_approve_if"].([]any)
	firstPolicy := policies[0].(map[string]any)["policy"].(map[string]any)
	if firstPolicy["type"] != "exact_actions" || firstPolicy["actions"] == nil || firstPolicy["argument_schema"] != nil ||
		!strings.Contains(string(encoded), `"argument_schema":{"required":["name"],"type":"object"}`) {
		t.Fatalf("Action policy JSON shape = %s", encoded)
	}
}

func TestActionPolicyReaderReturnsPythonUnavailableCodesAndSanitizedFailures(t *testing.T) {
	proxy := &fakeActionProxy{}
	federator := &fakeActionFederator{token: "token"}
	reader, err := NewActionPolicyReader(federator, proxy, "https://actions.example.test?secret=hidden")
	if err != nil {
		t.Fatal(err)
	}
	subject := &sandboxespb.ServiceAccount{Namespace: "team", Name: "worker"}

	_, unavailable, err := reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerToken}, actionSession(), subject)
	if err != nil || unavailable.Code != "operator_session_required" || federator.calls != 0 {
		t.Fatalf("workload action policy = %#v, %v; exchange calls=%d", unavailable, err, federator.calls)
	}
	_, unavailable, err = reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, &auth.RequestSession{}, subject)
	if err != nil || unavailable.Code != "operator_reauthentication_required" {
		t.Fatalf("missing login = %#v, %v", unavailable, err)
	}
	_, unavailable, err = (&ActionPolicyReader{}).ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(), subject)
	if err != nil || unavailable.Code != "operator_federation_not_configured" {
		t.Fatalf("missing federation = %#v, %v", unavailable, err)
	}
	noProxy, err := NewActionPolicyReader(federator, nil, "")
	if err != nil {
		t.Fatal(err)
	}
	_, unavailable, err = noProxy.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(), subject)
	if err != nil || unavailable.Code != "action_facade_not_configured" {
		t.Fatalf("missing proxy = %#v, %v", unavailable, err)
	}

	federator.err = &federation.FederationError{Code: "operator_federation_not_configured", Status: http.StatusServiceUnavailable}
	_, unavailable, err = reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(), subject)
	if err != nil || unavailable.Code != "operator_federation_not_configured" || unavailable.Upstream != nil {
		t.Fatalf("federation refusal = %#v, %v", unavailable, err)
	}
	federator.err = &federation.UpstreamError{
		Method: http.MethodPost, URL: "https://issuer.example.test/token", UpstreamStatus: http.StatusBadGateway, ErrorType: "HTTPStatusError",
	}
	_, unavailable, err = reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(), subject)
	if err != nil || unavailable.Code != "upstream_request_failed" || unavailable.Upstream == nil ||
		unavailable.Upstream.UpstreamStatus == nil || *unavailable.Upstream.UpstreamStatus != http.StatusBadGateway {
		t.Fatalf("federation upstream failure = %#v, %v", unavailable, err)
	}
	federator.err = nil
	proxy.response = &http.Response{
		StatusCode: http.StatusBadGateway,
		Body:       io.NopCloser(strings.NewReader("response contains a secret and must not leak")),
	}
	_, unavailable, err = reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(), subject)
	if err != nil || unavailable.Code != "upstream_request_failed" || unavailable.Upstream == nil ||
		unavailable.Upstream.UpstreamStatus == nil || *unavailable.Upstream.UpstreamStatus != http.StatusBadGateway ||
		unavailable.Upstream.URL != "https://actions.example.test/v1/action-policy" {
		t.Fatalf("Action Service failure = %#v, %v", unavailable, err)
	}
	encoded, err := json.Marshal(unavailable)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "secret") || strings.Contains(string(encoded), "hidden") ||
		strings.Contains(string(encoded), "response contains") {
		t.Fatalf("failure details leaked private data: %s", encoded)
	}
}

type barrierListClient struct {
	client.Client
	mu      sync.Mutex
	started int
	ready   chan struct{}
}

func (c *barrierListClient) List(ctx context.Context, list client.ObjectList, options ...client.ListOption) error {
	c.mu.Lock()
	c.started++
	if c.started == 3 {
		close(c.ready)
	}
	ready := c.ready
	c.mu.Unlock()
	select {
	case <-ready:
		return c.Client.List(ctx, list, options...)
	case <-ctx.Done():
		return ctx.Err()
	}
}

func TestBindingsForStartsAllKubernetesReadsConcurrently(t *testing.T) {
	base := clientfake.NewClientBuilder().WithScheme(policyScheme(t)).Build()
	objects := &barrierListClient{Client: base, ready: make(chan struct{})}
	access := New(testNamespace, objects)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := access.BindingsFor(ctx, &sandboxespb.ServiceAccount{Namespace: testNamespace, Name: "worker"}); err != nil {
		t.Fatal(err)
	}
	if objects.started != 3 {
		t.Fatalf("Kubernetes list calls = %d, want concurrent calls for all three resources", objects.started)
	}
}

func TestActionPolicyReaderMapsProxyTransportFailures(t *testing.T) {
	upstream := &fakeActionProxy{err: errors.New("connection refused")}
	reader, err := NewActionPolicyReader(&fakeActionFederator{token: "token"}, upstream, "https://actions.example.test")
	if err != nil {
		t.Fatal(err)
	}
	_, unavailable, err := reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(),
		&sandboxespb.ServiceAccount{Namespace: "team", Name: "worker"})
	if err != nil || unavailable.Code != "upstream_request_failed" || unavailable.Upstream == nil ||
		unavailable.Upstream.ErrorType != "errorString" {
		t.Fatalf("transport failure = %#v, %v", unavailable, err)
	}
}

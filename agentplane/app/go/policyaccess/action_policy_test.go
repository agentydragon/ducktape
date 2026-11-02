package policyaccess

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/federation"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
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

type fakeActionPolicyRPC struct {
	response    *action_servicepb.GetActionPolicyResponse
	err         error
	request     *action_servicepb.GetActionPolicyRequest
	bearer      []string
	hasDeadline bool
	deadlineIn  time.Duration
	call        func(context.Context, *action_servicepb.GetActionPolicyRequest) (*action_servicepb.GetActionPolicyResponse, error)
}

func (f *fakeActionPolicyRPC) GetActionPolicy(
	ctx context.Context,
	request *action_servicepb.GetActionPolicyRequest,
	_ ...grpc.CallOption,
) (*action_servicepb.GetActionPolicyResponse, error) {
	f.request = request
	md, _ := metadata.FromOutgoingContext(ctx)
	f.bearer = md.Get("authorization")
	if deadline, ok := ctx.Deadline(); ok {
		f.hasDeadline = true
		f.deadlineIn = time.Until(deadline)
	}
	if f.call != nil {
		return f.call(ctx, request)
	}
	return f.response, f.err
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
	actions := &fakeActionPolicyRPC{response: &action_servicepb.GetActionPolicyResponse{ActionPolicy: policy}}
	reader, err := NewActionPolicyReader(federator, actions, "https://actions.example.test/?api_key=must-not-leak#fragment")
	if err != nil {
		t.Fatal(err)
	}
	view, unavailable, err := reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(), &identitypb.ServiceAccount{
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
	if federator.calls != 1 || actions.request == nil || actions.request.GetSubject().GetNamespace() != "team-a" ||
		actions.request.GetSubject().GetName() != "worker" || !reflect.DeepEqual(actions.bearer, []string{"Bearer short-lived-target-token"}) ||
		!actions.hasDeadline || actions.deadlineIn <= 0 || actions.deadlineIn > actionPolicyRequestTimeout {
		t.Fatalf("federated RPC: calls=%d request=%#v bearer=%v", federator.calls, actions.request, actions.bearer)
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

func TestActionPolicyReaderPropagatesCancellation(t *testing.T) {
	started := make(chan struct{})
	reader, err := NewActionPolicyReader(&fakeActionFederator{token: "token"}, &fakeActionPolicyRPC{
		call: func(ctx context.Context, _ *action_servicepb.GetActionPolicyRequest) (*action_servicepb.GetActionPolicyResponse, error) {
			close(started)
			<-ctx.Done()
			return nil, status.Error(codes.Canceled, "request cancelled")
		},
	}, "https://actions.example.test")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, _, callErr := reader.ForSubject(ctx, auth.Caller{Kind: auth.CallerOperator}, actionSession(),
			&identitypb.ServiceAccount{Namespace: "team", Name: "worker"})
		done <- callErr
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("ForSubject() did not start the Action Service RPC")
	}
	cancel()
	select {
	case got := <-done:
		if !errors.Is(got, context.Canceled) {
			t.Fatalf("ForSubject() cancellation error = %v, want context.Canceled", got)
		}
	case <-time.After(time.Second):
		t.Fatal("ForSubject() did not stop after cancellation")
	}
}

func TestActionPolicyReaderReturnsPythonUnavailableCodesAndSanitizedFailures(t *testing.T) {
	actions := &fakeActionPolicyRPC{}
	federator := &fakeActionFederator{token: "token"}
	reader, err := NewActionPolicyReader(federator, actions, "https://actions.example.test?secret=hidden")
	if err != nil {
		t.Fatal(err)
	}
	subject := &identitypb.ServiceAccount{Namespace: "team", Name: "worker"}

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
	noActions, err := NewActionPolicyReader(federator, nil, "")
	if err != nil {
		t.Fatal(err)
	}
	_, unavailable, err = noActions.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(), subject)
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
	actions.err = status.Error(codes.PermissionDenied, "response contains a secret and must not leak")
	_, unavailable, err = reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(), subject)
	if err != nil || unavailable.Code != "upstream_request_failed" || unavailable.Upstream == nil ||
		unavailable.Upstream.UpstreamStatus == nil || *unavailable.Upstream.UpstreamStatus != http.StatusForbidden ||
		unavailable.Upstream.URL != "https://actions.example.test/v1/action-policy" {
		t.Fatalf("Action Service RPC failure = %#v, %v", unavailable, err)
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

func TestActionPolicyReaderCancelsTheGeneratedRPCWithItsRequest(t *testing.T) {
	entered := make(chan struct{})
	actions := &fakeActionPolicyRPC{call: func(ctx context.Context, _ *action_servicepb.GetActionPolicyRequest) (*action_servicepb.GetActionPolicyResponse, error) {
		close(entered)
		<-ctx.Done()
		return nil, ctx.Err()
	}}
	reader, err := NewActionPolicyReader(&fakeActionFederator{token: "token"}, actions, "https://actions.example.test")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	type result struct {
		view        *ActionPolicyView
		unavailable *ActionPolicyUnavailable
		err         error
	}
	done := make(chan result, 1)
	go func() {
		view, unavailable, err := reader.ForSubject(ctx, auth.Caller{Kind: auth.CallerOperator}, actionSession(),
			&identitypb.ServiceAccount{Namespace: "team", Name: "worker"})
		done <- result{view: view, unavailable: unavailable, err: err}
	}()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("Action Service RPC did not start")
	}
	cancel()
	select {
	case outcome := <-done:
		if outcome.view != nil || outcome.unavailable != nil || !errors.Is(outcome.err, context.Canceled) {
			t.Fatalf("cancelled RPC result = %+v", outcome)
		}
	case <-time.After(time.Second):
		t.Fatal("Action Service RPC did not return after request cancellation")
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
	if _, err := access.BindingsFor(ctx, &identitypb.ServiceAccount{Namespace: testNamespace, Name: "worker"}); err != nil {
		t.Fatal(err)
	}
	if objects.started != 3 {
		t.Fatalf("Kubernetes list calls = %d, want concurrent calls for all three resources", objects.started)
	}
}

func TestActionPolicyReaderMapsProxyTransportFailures(t *testing.T) {
	actions := &fakeActionPolicyRPC{err: status.Error(codes.Unavailable, "connection refused")}
	reader, err := NewActionPolicyReader(&fakeActionFederator{token: "token"}, actions, "https://actions.example.test")
	if err != nil {
		t.Fatal(err)
	}
	_, unavailable, err := reader.ForSubject(context.Background(), auth.Caller{Kind: auth.CallerOperator}, actionSession(),
		&identitypb.ServiceAccount{Namespace: "team", Name: "worker"})
	if err != nil || unavailable.Code != "upstream_request_failed" || unavailable.Upstream == nil ||
		unavailable.Upstream.ErrorType != "GRPCStatusError" || unavailable.Upstream.UpstreamStatus == nil ||
		*unavailable.Upstream.UpstreamStatus != http.StatusServiceUnavailable {
		t.Fatalf("transport failure = %#v, %v", unavailable, err)
	}
}

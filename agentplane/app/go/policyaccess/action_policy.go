package policyaccess

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"reflect"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/federation"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"google.golang.org/protobuf/encoding/protojson"
)

// ActionFederator exchanges the browser login in an app request for a scoped Action Service token.
type ActionFederator interface {
	Exchange(context.Context, federation.SessionLoginRow) (string, error)
}

// ActionPolicyUpstream opens the generated Action Service facade request. *proxy.UpstreamProxy
// satisfies this interface.
type ActionPolicyUpstream interface {
	OpenRequest(context.Context, string, string, string, string, http.Header, io.Reader) (*http.Response, error)
}

// ActionPolicyReader asks the Action Service for the effective policy of an explicit subject.
type ActionPolicyReader struct {
	federator  ActionFederator
	upstream   ActionPolicyUpstream
	requestURL string
}

// NewActionPolicyReader creates the federated reader. serviceURL is the configured Action Service
// URL and is retained only for credential-free failure metadata.
func NewActionPolicyReader(
	federator ActionFederator,
	upstream ActionPolicyUpstream,
	serviceURL string,
) (*ActionPolicyReader, error) {
	reader := &ActionPolicyReader{federator: federator, upstream: upstream}
	if isNilDependency(upstream) {
		return reader, nil
	}
	base, err := url.Parse(serviceURL)
	if err != nil || !base.IsAbs() || base.Host == "" || (base.Scheme != "http" && base.Scheme != "https") {
		return nil, errors.New("Action Service URL must be an absolute HTTP(S) URL")
	}
	base.User = nil
	base.Path = "/v1/action-policy"
	base.RawPath = ""
	base.RawQuery = ""
	base.Fragment = ""
	reader.requestURL = base.String()
	return reader, nil
}

// ActionPolicyView is the Action Service's answer for a subject: what it would evaluate at admission.
type ActionPolicyView struct {
	Synced        bool                  `json:"synced"`
	AutoApproveIf []EffectivePolicyView `json:"auto_approve_if"`
}

// ActionPolicyUnavailable carries a fixed public reason rather than presenting an unavailable answer as empty.
type ActionPolicyUnavailable struct {
	Kind     string                 `json:"kind"`
	Code     string                 `json:"code"`
	Upstream *ActionUpstreamFailure `json:"upstream"`
}

// ActionUpstreamFailure contains no query, credential, or provider response text.
type ActionUpstreamFailure struct {
	Method         string `json:"method"`
	URL            string `json:"url"`
	UpstreamStatus *int   `json:"upstream_status"`
	ErrorType      string `json:"error_type"`
}

// EffectivePolicyView is one policy in the Action Service's evaluation order.
type EffectivePolicyView struct {
	Binding   string         `json:"binding"`
	PolicySet string         `json:"policy_set"`
	Source    string         `json:"source"`
	Index     int32          `json:"index"`
	Policy    ActionRuleView `json:"policy"`
}

// ActionRuleView is a concrete representation of the Action Service's tagged Policy union.
// Only fields for Type are emitted in JSON.
type ActionRuleView struct {
	Type           string              `json:"type"`
	Actions        map[string][]string `json:"actions,omitempty"`
	ArgumentSchema map[string]any      `json:"argument_schema,omitempty"`
	Owner          string              `json:"owner,omitempty"`
	Repository     string              `json:"repository,omitempty"`
	Entities       map[string][]string `json:"entities,omitempty"`
}

func (p ActionRuleView) MarshalJSON() ([]byte, error) {
	switch p.Type {
	case "exact_actions", "github_public_repository":
		return json.Marshal(struct {
			Type    string              `json:"type"`
			Actions map[string][]string `json:"actions"`
		}{Type: p.Type, Actions: p.Actions})
	case "argument_schema":
		if p.ArgumentSchema == nil {
			p.ArgumentSchema = map[string]any{}
		}
		return json.Marshal(struct {
			Type           string              `json:"type"`
			Actions        map[string][]string `json:"actions"`
			ArgumentSchema map[string]any      `json:"argument_schema"`
		}{Type: p.Type, Actions: p.Actions, ArgumentSchema: p.ArgumentSchema})
	case "github_repository":
		return json.Marshal(struct {
			Type       string              `json:"type"`
			Actions    map[string][]string `json:"actions"`
			Owner      string              `json:"owner"`
			Repository string              `json:"repository"`
		}{Type: p.Type, Actions: p.Actions, Owner: p.Owner, Repository: p.Repository})
	case "home_assistant_entity_control":
		return json.Marshal(struct {
			Type     string              `json:"type"`
			Actions  map[string][]string `json:"actions"`
			Entities map[string][]string `json:"entities"`
		}{Type: p.Type, Actions: p.Actions, Entities: p.Entities})
	default:
		return nil, fmt.Errorf("Action Service returned a policy without a recognized kind")
	}
}

// ForSubject returns either the effective Action policy or the same fixed unavailability frame
// the Python live stream emits. Unexpected decoding/configuration errors are returned as errors.
func (r *ActionPolicyReader) ForSubject(
	ctx context.Context,
	caller auth.Caller,
	session *auth.RequestSession,
	subject *sandboxespb.ServiceAccount,
) (*ActionPolicyView, *ActionPolicyUnavailable, error) {
	if subject == nil || subject.GetNamespace() == "" || subject.GetName() == "" {
		return nil, nil, errors.New("subject must set both namespace and name")
	}
	if caller.Kind != auth.CallerOperator {
		return nil, unavailable("operator_session_required", nil), nil
	}
	if isNilDependency(r.federator) {
		return nil, unavailable("operator_federation_not_configured", nil), nil
	}
	if session == nil || session.Login == nil || session.Row == nil {
		return nil, unavailable("operator_reauthentication_required", nil), nil
	}
	bearer, err := r.federator.Exchange(ctx, session.Row)
	if err != nil {
		if errors.Is(ctx.Err(), context.Canceled) {
			return nil, nil, context.Canceled
		}
		var federationErr *federation.FederationError
		if errors.As(err, &federationErr) {
			return nil, unavailable(federationErr.Code, nil), nil
		}
		var upstreamErr *federation.UpstreamError
		if errors.As(err, &upstreamErr) {
			return nil, unavailable("upstream_request_failed", &ActionUpstreamFailure{
				Method: upstreamErr.Method, URL: upstreamErr.URL,
				UpstreamStatus: optionalStatus(upstreamErr.UpstreamStatus), ErrorType: upstreamErr.ErrorType,
			}), nil
		}
		return nil, nil, err
	}
	if isNilDependency(r.upstream) {
		return nil, unavailable("action_facade_not_configured", nil), nil
	}
	query := "subject.namespace=" + url.QueryEscape(subject.GetNamespace()) + "&subject.name=" + url.QueryEscape(subject.GetName())
	response, err := r.upstream.OpenRequest(ctx, http.MethodGet, "v1/action-policy", query, bearer,
		http.Header{"Accept": []string{"application/json"}}, nil)
	if err != nil {
		if errors.Is(ctx.Err(), context.Canceled) {
			return nil, nil, context.Canceled
		}
		return nil, unavailable("upstream_request_failed", &ActionUpstreamFailure{
			Method: http.MethodGet, URL: r.requestURL, ErrorType: safeErrorType(err),
		}), nil
	}
	if response == nil {
		return nil, nil, errors.New("Action Service proxy returned no response")
	}
	if response.Body == nil {
		return nil, nil, errors.New("Action Service proxy returned a response without a body")
	}
	defer response.Body.Close()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return nil, unavailable("upstream_request_failed", &ActionUpstreamFailure{
			Method: http.MethodGet, URL: r.requestURL, UpstreamStatus: optionalStatus(response.StatusCode), ErrorType: "HTTPStatusError",
		}), nil
	}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		if errors.Is(ctx.Err(), context.Canceled) {
			return nil, nil, context.Canceled
		}
		return nil, unavailable("upstream_request_failed", &ActionUpstreamFailure{
			Method: http.MethodGet, URL: r.requestURL, ErrorType: safeErrorType(err),
		}), nil
	}
	parsed := &action_servicepb.GetActionPolicyResponse{}
	if err := protojson.Unmarshal(body, parsed); err != nil {
		return nil, nil, fmt.Errorf("parse Action Service policy response: %w", err)
	}
	if parsed.GetActionPolicy() == nil {
		return nil, nil, errors.New("Action Service returned no action policy")
	}
	view, err := actionPolicyView(parsed.GetActionPolicy())
	if err != nil {
		return nil, nil, err
	}
	return view, nil, nil
}

func actionPolicyView(policy *action_servicepb.CallerActionPolicy) (*ActionPolicyView, error) {
	view := &ActionPolicyView{
		Synced: policy.GetSynced(), AutoApproveIf: make([]EffectivePolicyView, 0, len(policy.GetAutoApproveIf())),
	}
	for _, effective := range policy.GetAutoApproveIf() {
		if effective == nil {
			return nil, errors.New("Action Service returned an empty effective policy")
		}
		sourceName, ok := action_servicepb.PolicySource_name[int32(effective.GetSource())]
		if !ok || sourceName != "POLICY_SOURCE_RULES" {
			return nil, fmt.Errorf("Action Service returned an unknown policy source %d", effective.GetSource())
		}
		source := strings.TrimPrefix(sourceName, "POLICY_SOURCE_")
		projected, err := actionPolicyRule(effective.GetPolicy())
		if err != nil {
			return nil, err
		}
		view.AutoApproveIf = append(view.AutoApproveIf, EffectivePolicyView{
			Binding: effective.GetBinding(), PolicySet: effective.GetPolicySet(), Source: strings.ToLower(source),
			Index: effective.GetIndex(), Policy: projected,
		})
	}
	return view, nil
}

// ActionPolicyViewFromProto projects the generated Action Service response to the app's
// credential-free browser view. Keep the wire protobuf private: it includes binding diagnostics
// that the Python /caller-grants response intentionally does not expose.
func ActionPolicyViewFromProto(policy *action_servicepb.CallerActionPolicy) (*ActionPolicyView, error) {
	if policy == nil {
		return nil, errors.New("Action Service returned no action policy")
	}
	return actionPolicyView(policy)
}

func actionPolicyRule(policy *action_servicepb.Policy) (ActionRuleView, error) {
	if policy == nil {
		return ActionRuleView{}, errors.New("Action Service returned a policy without a recognized kind")
	}
	switch kind := policy.GetKind().(type) {
	case *action_servicepb.Policy_ExactActions:
		return ActionRuleView{Type: "exact_actions", Actions: actionNames(kind.ExactActions.GetActions())}, nil
	case *action_servicepb.Policy_ArgumentSchema:
		var schema map[string]any
		if kind.ArgumentSchema.GetArgumentSchema() != nil {
			schema = kind.ArgumentSchema.GetArgumentSchema().AsMap()
		} else {
			schema = map[string]any{}
		}
		return ActionRuleView{
			Type: "argument_schema", Actions: actionNames(kind.ArgumentSchema.GetActions()), ArgumentSchema: schema,
		}, nil
	case *action_servicepb.Policy_GithubRepository:
		return ActionRuleView{
			Type: "github_repository", Actions: actionNames(kind.GithubRepository.GetActions()),
			Owner: kind.GithubRepository.GetOwner(), Repository: kind.GithubRepository.GetRepository(),
		}, nil
	case *action_servicepb.Policy_GithubPublicRepository:
		return ActionRuleView{Type: "github_public_repository", Actions: actionNames(kind.GithubPublicRepository.GetActions())}, nil
	case *action_servicepb.Policy_HomeAssistantEntityControl:
		return ActionRuleView{
			Type: "home_assistant_entity_control", Actions: actionNames(kind.HomeAssistantEntityControl.GetActions()),
			Entities: actionNames(kind.HomeAssistantEntityControl.GetEntities()),
		}, nil
	default:
		return ActionRuleView{}, errors.New("Action Service returned a policy without a recognized kind")
	}
}

func actionNames(named map[string]*action_servicepb.StringList) map[string][]string {
	result := make(map[string][]string, len(named))
	for key, list := range named {
		if list == nil {
			result[key] = []string{}
			continue
		}
		result[key] = append([]string(nil), list.GetValues()...)
	}
	return result
}

func unavailable(code string, upstream *ActionUpstreamFailure) *ActionPolicyUnavailable {
	return &ActionPolicyUnavailable{Kind: "unavailable", Code: code, Upstream: upstream}
}

func optionalStatus(status int) *int {
	if status == 0 {
		return nil
	}
	return &status
}

func safeErrorType(err error) string {
	var networkErr net.Error
	if errors.As(err, &networkErr) {
		return "TransportError"
	}
	if kind := reflect.TypeOf(err); kind != nil {
		for kind.Kind() == reflect.Pointer {
			kind = kind.Elem()
		}
		return kind.Name()
	}
	return "RequestError"
}

func isNilDependency(dependency any) bool {
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

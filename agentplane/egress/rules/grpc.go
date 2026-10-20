package rules

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

var (
	errInvalidBearer       = errors.New("invalid workload bearer")
	errIdentityUnavailable = errors.New("workload identity unavailable")
)

// Service is the rules API. The caller is the ServiceAccount its bearer proves to the Verifier,
// whose hop token and projected tokens it accepts, and nothing in the request selects another.
type Service struct {
	egressrulespb.UnimplementedEgressRulesServer
	verifier *identity.Verifier
	index    *policy.Index
}

func NewService(verifier *identity.Verifier, index *policy.Index) *Service {
	return &Service{verifier: verifier, index: index}
}

func (s *Service) GetRules(ctx context.Context, _ *egressrulespb.GetRulesRequest) (*egressrulespb.GetRulesResponse, error) {
	incoming, _ := metadata.FromIncomingContext(ctx)
	subject, err := subjectOf(ctx, s.verifier, incoming.Get("authorization"))
	switch {
	case errors.Is(err, errIdentityUnavailable):
		return nil, status.Error(codes.Unavailable, err.Error())
	case err != nil:
		return nil, status.Error(codes.Unauthenticated, err.Error())
	}
	response, err := responseOf(subject, s.index.ListEffectivePolicies(subject))
	if err != nil {
		slog.Error("egress rules not presentable", "error", err)
		return nil, status.Error(codes.Internal, "egress rules not presentable")
	}
	return response, nil
}

// subjectOf is the ServiceAccount that the authorization values of a call prove: errInvalidBearer when
// they prove none, so a response never says which part was wrong with it, errIdentityUnavailable when
// the bearer could not be reviewed.
func subjectOf(ctx context.Context, verifier *identity.Verifier, authorization []string) (policyapi.ServiceAccountRef, error) {
	token, ok := identity.BearerToken(authorization)
	if !ok {
		return policyapi.ServiceAccountRef{}, errInvalidBearer
	}
	principal, err := verifier.Identify(ctx, token)
	if err != nil {
		var rejection *identity.Rejection
		if errors.As(err, &rejection) && rejection.Reason == decision.Unavailable {
			slog.Warn("workload token could not be reviewed", "error", err)
			return policyapi.ServiceAccountRef{}, errIdentityUnavailable
		}
		return policyapi.ServiceAccountRef{}, errInvalidBearer
	}
	return policyapi.ServiceAccountRef{Namespace: principal.Account.Namespace, Name: principal.Account.Name}, nil
}

// responseOf is everything subject may reach, as the proxy would decide it right now. It is built field
// by field from policy.EffectivePolicy and never from a resource object, so a field added to an
// EgressCredential, a second source say, reaches a sandbox only when someone writes it in here.
func responseOf(subject policyapi.ServiceAccountRef, policies []policy.EffectivePolicy) (*egressrulespb.GetRulesResponse, error) {
	response := &egressrulespb.GetRulesResponse{
		Subject:  &sandboxespb.ServiceAccount{Namespace: subject.Namespace, Name: subject.Name},
		Policies: make([]*egressrulespb.Policy, len(policies)),
	}
	for i, effective := range policies {
		granted := &egressrulespb.Policy{Name: effective.Name, Rules: make([]*egressrulespb.Rule, len(effective.Rules))}
		for j, rule := range effective.Rules {
			var err error
			if granted.Rules[j], err = ruleOf(rule); err != nil {
				return nil, err
			}
		}
		response.Policies[i] = granted
	}
	return response, nil
}

func ruleOf(effective policy.EffectiveRule) (*egressrulespb.Rule, error) {
	rule := &egressrulespb.Rule{Hosts: effective.Rule.Hosts, Methods: stringListOf(effective.Rule.Methods)}
	if effective.Rule.Paths != nil {
		patterns := make([]string, len(effective.Rule.Paths))
		for i, glob := range effective.Rule.Paths {
			patterns[i] = glob.Pattern
		}
		rule.Paths = stringListOf(patterns)
	}
	if credential := effective.Credential; credential != nil {
		rule.Credential = &egressrulespb.Credential{
			Name: credential.Name, Description: credential.Description, Placeholder: credential.Placeholder(),
			Targets: make([]*egressrulespb.Target, len(credential.Targets)),
		}
		for i, target := range credential.Targets {
			var err error
			if rule.Credential.Targets[i], err = targetOf(target.ToAPI()); err != nil {
				return nil, err
			}
		}
	}
	return rule, nil
}

// stringListOf keeps a nil list absent, which the rules read as "any".
func stringListOf(values []string) *egressrulespb.StringList {
	if values == nil {
		return nil
	}
	return &egressrulespb.StringList{Values: values}
}

// targetOf carries presentation syntax only: egressapi.Target holds no value and no source.
func targetOf(target egressapi.Target) (*egressrulespb.Target, error) {
	method, err := targetMethodOf(target.Method)
	if err != nil {
		return nil, err
	}
	presented := &egressrulespb.Target{Method: method}
	if target.Header != "" {
		presented.Header = proto.String(target.Header)
	}
	if target.Field != "" {
		presented.Field = proto.String(target.Field)
	}
	if target.Scheme != "" {
		presented.Scheme = proto.String(target.Scheme)
	}
	return presented, nil
}

func targetMethodOf(method egressapi.TargetMethod) (egressrulespb.Target_Method, error) {
	switch method {
	case egressapi.WholeValue:
		return egressrulespb.Target_METHOD_WHOLE_VALUE, nil
	case egressapi.SchemeToken:
		return egressrulespb.Target_METHOD_SCHEME_TOKEN, nil
	case egressapi.BasicUsername:
		return egressrulespb.Target_METHOD_BASIC_USERNAME, nil
	case egressapi.BasicPassword:
		return egressrulespb.Target_METHOD_BASIC_PASSWORD, nil
	case egressapi.BasicWhole:
		return egressrulespb.Target_METHOD_BASIC_WHOLE, nil
	case egressapi.JSONField:
		return egressrulespb.Target_METHOD_JSON_FIELD, nil
	}
	return 0, fmt.Errorf("target method %q has no gRPC counterpart", method)
}

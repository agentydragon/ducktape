package rules

import (
	"context"
	"fmt"
	"log/slog"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Service projects the admitted caller's effective egress rules.
type Service struct {
	egressrulespb.UnimplementedEgressRulesServer
	index *policy.Index
}

func NewService(index *policy.Index) *Service { return &Service{index: index} }

func (s *Service) GetRules(ctx context.Context, _ *egressrulespb.GetRulesRequest) (*egressrulespb.GetRulesResponse, error) {
	principal, err := auth.Caller(ctx)
	if err != nil {
		return nil, err
	}
	subject := policyapi.ServiceAccountRef{Namespace: principal.Account.Namespace, Name: principal.Account.Name}
	response, err := responseOf(subject, s.index.ListEffectivePolicies(reader.ServiceAccount(subject)))
	if err != nil {
		slog.Error("egress rules not presentable", "error", err)
		return nil, status.Error(codes.Internal, "egress rules not presentable")
	}
	return response, nil
}

// responseOf is everything subject may reach, as the proxy would decide it right now. It is built field
// by field from policy.EffectivePolicy and never from a resource object, so a field added to an
// EgressCredential, a second source say, reaches a sandbox only when someone writes it in here.
func responseOf(subject policyapi.ServiceAccountRef, policies []policy.EffectivePolicy) (*egressrulespb.GetRulesResponse, error) {
	response := &egressrulespb.GetRulesResponse{
		Subject:  &identitypb.ServiceAccount{Namespace: subject.Namespace, Name: subject.Name},
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
		var err error
		rule.Credential, err = CredentialOf(*credential)
		if err != nil {
			return nil, err
		}
	}
	return rule, nil
}

// CredentialOf exposes only presentation metadata and the account an impersonation credential acts
// as, never a Secret reference or a value.
func CredentialOf(credential resources.Credential) (*egressrulespb.Credential, error) {
	out := &egressrulespb.Credential{Uid: credential.UID, Name: credential.Name, Description: credential.Description, Placeholder: credential.Placeholder(), Targets: make([]*egressrulespb.Target, len(credential.Targets))}
	if account, ok := credential.Source.(resources.KubernetesServiceAccount); ok {
		out.KubernetesServiceAccount = &identitypb.ServiceAccount{Namespace: account.Namespace, Name: account.Name}
	}
	for i, target := range credential.Targets {
		var err error
		if out.Targets[i], err = targetOf(target.ToAPI()); err != nil {
			return nil, err
		}
	}
	return out, nil
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

package rules

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/proto/egress_rules"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Service is the rules API's `GET /v1/rules` as gRPC. It authenticates as that API does: the caller
// is the ServiceAccount the bearer proves to the same Verifier, whose hop token and projected
// tokens it accepts, and nothing in the request selects another.
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
	response, err := responseOf(ViewOf(subject, s.index.ListEffectivePolicies(subject)))
	if err != nil {
		slog.Error("egress rules not presentable", "error", err)
		return nil, status.Error(codes.Internal, "egress rules not presentable")
	}
	return response, nil
}

func responseOf(view EgressView) (*egressrulespb.GetRulesResponse, error) {
	response := &egressrulespb.GetRulesResponse{
		Subject:  &egressrulespb.Subject{Namespace: view.Subject.Namespace, Name: view.Subject.Name},
		Policies: make([]*egressrulespb.Policy, len(view.Policies)),
	}
	for i, policyView := range view.Policies {
		response.Policies[i] = &egressrulespb.Policy{Name: policyView.Name, Rules: make([]*egressrulespb.Rule, len(policyView.Rules))}
		for j, ruleView := range policyView.Rules {
			rule := &egressrulespb.Rule{Hosts: ruleView.Hosts, Methods: stringListOf(ruleView.Methods), Paths: stringListOf(ruleView.Paths)}
			if credential := ruleView.Credential; credential != nil {
				rule.Credential = &egressrulespb.Credential{
					Name: credential.Name, Description: credential.Description, Placeholder: credential.Placeholder,
					Targets: make([]*egressrulespb.Target, len(credential.Targets)),
				}
				for k, target := range credential.Targets {
					method, err := targetMethodOf(target.Method)
					if err != nil {
						return nil, err
					}
					rule.Credential.Targets[k] = &egressrulespb.Target{Method: method}
					if target.Header != "" {
						rule.Credential.Targets[k].Header = proto.String(target.Header)
					}
					if target.Field != "" {
						rule.Credential.Targets[k].Field = proto.String(target.Field)
					}
					if target.Scheme != "" {
						rule.Credential.Targets[k].Scheme = proto.String(target.Scheme)
					}
				}
			}
			response.Policies[i].Rules[j] = rule
		}
	}
	return response, nil
}

// stringListOf keeps a nil list absent, which the rules read as "any".
func stringListOf(values []string) *egressrulespb.StringList {
	if values == nil {
		return nil
	}
	return &egressrulespb.StringList{Values: values}
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

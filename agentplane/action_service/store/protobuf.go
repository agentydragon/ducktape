package store

import (
	"fmt"
	"math"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"google.golang.org/protobuf/proto"
)

var policySources = map[string]action_servicepb.PolicySource{
	PolicySourceRules: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
}

var policyKindProtos = map[PolicyKind]action_servicepb.PolicyKind{
	PolicyExactActions:               action_servicepb.PolicyKind_POLICY_KIND_EXACT_ACTIONS,
	PolicyArgumentSchema:             action_servicepb.PolicyKind_POLICY_KIND_ARGUMENT_SCHEMA,
	PolicyGitHubRepository:           action_servicepb.PolicyKind_POLICY_KIND_GITHUB_REPOSITORY,
	PolicyGitHubPublicRepository:     action_servicepb.PolicyKind_POLICY_KIND_GITHUB_PUBLIC_REPOSITORY,
	PolicyHomeAssistantEntityControl: action_servicepb.PolicyKind_POLICY_KIND_HOME_ASSISTANT_ENTITY_CONTROL,
}

func encodeRecordedPrincipal(principal *action_servicepb.Principal) ([]byte, error) {
	if principal == nil {
		return nil, nil
	}
	return proto.Marshal(principal)
}

// Proto converts the store's policy evidence to its public message form. Keeping this
// mapping beside the storage model lets both the API and the binary storage codec use
// the same canonical conversion.
func (evidence PolicyEvidence) Proto() (*action_servicepb.PolicyEvidence, error) {
	source, ok := policySources[evidence.Matched.Source]
	if !ok {
		return nil, fmt.Errorf("unknown policy source %q", evidence.Matched.Source)
	}
	kind, ok := policyKindProtos[evidence.Matched.Type]
	if !ok {
		return nil, fmt.Errorf("unknown policy kind %q", evidence.Matched.Type)
	}
	if evidence.Matched.Index > math.MaxInt32 {
		return nil, fmt.Errorf("policy index %d is outside int32", evidence.Matched.Index)
	}
	message := &action_servicepb.PolicyEvidence{Matched: &action_servicepb.MatchedPolicy{
		Namespace: evidence.Matched.Namespace,
		PolicySet: evidence.Matched.PolicySet,
		Source:    source,
		Index:     int32(evidence.Matched.Index),
		Type:      kind,
	}}
	for _, binding := range evidence.Bindings {
		message.Bindings = append(message.Bindings, &action_servicepb.BindingEvidence{
			Namespace: binding.Namespace, Name: binding.Name, ResourceVersion: binding.ResourceVersion,
		})
	}
	for _, set := range evidence.PolicySets {
		message.PolicySets = append(message.PolicySets, &action_servicepb.PolicySetEvidence{
			Namespace: set.Namespace, Name: set.Name, Generation: set.Generation,
		})
	}
	if repository := evidence.Matched.Repository; repository != nil {
		message.Matched.Repository = &action_servicepb.MatchedRepository{
			Owner: repository.Owner, Repository: repository.Repository, ConfirmedPublic: repository.ConfirmedPublic,
		}
	}
	return message, nil
}

func encodePolicyEvidence(evidence *PolicyEvidence) ([]byte, error) {
	if evidence == nil {
		return nil, nil
	}
	if evidence.Matched.Index < 0 {
		return nil, fmt.Errorf("policy index %d is negative", evidence.Matched.Index)
	}
	message, err := evidence.Proto()
	if err != nil {
		return nil, err
	}
	return proto.Marshal(message)
}

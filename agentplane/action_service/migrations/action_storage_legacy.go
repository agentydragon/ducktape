package migrations

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"regexp"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"google.golang.org/protobuf/proto"
)

// These private wire types describe the JSON written before Action storage version 6. Keep their
// decoding here so a later change to the service's public models cannot reinterpret stored history.
var historicalActionKey = regexp.MustCompile(`^[a-z][a-z0-9_-]*$`)

var historicalProto = proto.MarshalOptions{Deterministic: true}

var historicalPolicyKinds = map[string]action_servicepb.PolicyKind{
	"exact_actions":                 action_servicepb.PolicyKind_POLICY_KIND_EXACT_ACTIONS,
	"argument_schema":               action_servicepb.PolicyKind_POLICY_KIND_ARGUMENT_SCHEMA,
	"github_repository":             action_servicepb.PolicyKind_POLICY_KIND_GITHUB_REPOSITORY,
	"github_public_repository":      action_servicepb.PolicyKind_POLICY_KIND_GITHUB_PUBLIC_REPOSITORY,
	"home_assistant_entity_control": action_servicepb.PolicyKind_POLICY_KIND_HOME_ASSISTANT_ENTITY_CONTROL,
}

type historicalActionIdentity struct {
	Group string `json:"group"`
	Name  string `json:"name"`
}

func decodeHistoricalActionIdentity(data []byte) (historicalActionIdentity, error) {
	var identity historicalActionIdentity
	if err := decodeHistoricalFields(data, &identity, "group", "name"); err != nil {
		return identity, err
	}
	if len(identity.Group) > 200 || !historicalActionKey.MatchString(identity.Group) {
		return identity, fmt.Errorf("invalid action group %q", identity.Group)
	}
	if len(identity.Name) > 200 || !historicalActionKey.MatchString(identity.Name) {
		return identity, fmt.Errorf("invalid action name %q", identity.Name)
	}
	return identity, nil
}

func decodeHistoricalActor(data []byte) ([]byte, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return nil, err
	}
	var principal *action_servicepb.Principal
	if _, caller := fields["account"]; caller {
		var wire struct {
			Account json.RawMessage `json:"account"`
		}
		if err := decodeHistoricalFields(data, &wire, "account"); err != nil {
			return nil, err
		}
		var account struct {
			Namespace string `json:"namespace"`
			Name      string `json:"name"`
		}
		if err := decodeHistoricalFields(wire.Account, &account, "namespace", "name"); err != nil {
			return nil, err
		}
		if account.Namespace == "" || account.Name == "" {
			return nil, fmt.Errorf("caller account namespace and name are required")
		}
		principal = &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{
			Caller: &sandboxespb.ServiceAccount{Namespace: account.Namespace, Name: account.Name},
		}}
	} else {
		var operator struct {
			Issuer  string `json:"issuer"`
			Subject string `json:"subject"`
		}
		if err := decodeHistoricalFields(data, &operator, "issuer", "subject"); err != nil {
			return nil, err
		}
		if operator.Issuer == "" || operator.Subject == "" {
			return nil, fmt.Errorf("operator issuer and subject are required")
		}
		principal = &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{
			Operator: &action_servicepb.Operator{Issuer: operator.Issuer, Subject: operator.Subject},
		}}
	}
	return historicalProto.Marshal(principal)
}

type historicalEvidence struct {
	Bindings   []historicalBinding   `json:"bindings"`
	PolicySets []historicalPolicySet `json:"policy_sets"`
	Matched    historicalMatched     `json:"matched"`
}

type historicalBinding struct {
	Namespace       string `json:"namespace"`
	Name            string `json:"name"`
	ResourceVersion string `json:"resource_version"`
}

type historicalPolicySet struct {
	Namespace  string `json:"namespace"`
	Name       string `json:"name"`
	Generation int64  `json:"generation"`
}

type historicalMatched struct {
	Namespace  string                `json:"namespace"`
	PolicySet  string                `json:"policy_set"`
	Source     string                `json:"source"`
	Index      int64                 `json:"index"`
	Type       string                `json:"type"`
	Repository *historicalRepository `json:"repository"`
}

type historicalRepository struct {
	Owner           string `json:"owner"`
	Repository      string `json:"repository"`
	ConfirmedPublic bool   `json:"confirmed_public"`
}

func (v *historicalEvidence) UnmarshalJSON(data []byte) error {
	type plain historicalEvidence
	return decodeHistoricalFields(data, (*plain)(v), "bindings", "policy_sets", "matched")
}

func (v *historicalBinding) UnmarshalJSON(data []byte) error {
	type plain historicalBinding
	return decodeHistoricalFields(data, (*plain)(v), "namespace", "name", "resource_version")
}

func (v *historicalPolicySet) UnmarshalJSON(data []byte) error {
	type plain historicalPolicySet
	return decodeHistoricalFields(data, (*plain)(v), "namespace", "name", "generation")
}

func (v *historicalMatched) UnmarshalJSON(data []byte) error {
	type plain historicalMatched
	return decodeHistoricalFields(data, (*plain)(v), "namespace", "policy_set", "source", "index", "type")
}

func (v *historicalRepository) UnmarshalJSON(data []byte) error {
	type plain historicalRepository
	return decodeHistoricalFields(data, (*plain)(v), "owner", "repository", "confirmed_public")
}

func decodeHistoricalEvidence(data []byte) ([]byte, error) {
	var legacy historicalEvidence
	if err := json.Unmarshal(data, &legacy); err != nil {
		return nil, err
	}
	if legacy.Matched.Source != "rules" {
		return nil, fmt.Errorf("unknown policy source %q", legacy.Matched.Source)
	}
	kind, ok := historicalPolicyKinds[legacy.Matched.Type]
	if !ok {
		return nil, fmt.Errorf("unknown policy kind %q", legacy.Matched.Type)
	}
	if legacy.Matched.Index < 0 || legacy.Matched.Index > math.MaxInt32 {
		return nil, fmt.Errorf("policy index %d is outside int32", legacy.Matched.Index)
	}
	message := &action_servicepb.PolicyEvidence{Matched: &action_servicepb.MatchedPolicy{
		Namespace: legacy.Matched.Namespace,
		PolicySet: legacy.Matched.PolicySet,
		Source:    action_servicepb.PolicySource_POLICY_SOURCE_RULES,
		Index:     int32(legacy.Matched.Index),
		Type:      kind,
	}}
	for _, binding := range legacy.Bindings {
		message.Bindings = append(message.Bindings, &action_servicepb.BindingEvidence{
			Namespace: binding.Namespace, Name: binding.Name, ResourceVersion: binding.ResourceVersion,
		})
	}
	for _, set := range legacy.PolicySets {
		message.PolicySets = append(message.PolicySets, &action_servicepb.PolicySetEvidence{
			Namespace: set.Namespace, Name: set.Name, Generation: set.Generation,
		})
	}
	if repository := legacy.Matched.Repository; repository != nil {
		message.Matched.Repository = &action_servicepb.MatchedRepository{
			Owner: repository.Owner, Repository: repository.Repository, ConfirmedPublic: repository.ConfirmedPublic,
		}
	}
	return historicalProto.Marshal(message)
}

func decodeHistoricalFields(data []byte, value any, required ...string) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	if fields == nil {
		return fmt.Errorf("JSON object is required")
	}
	for _, name := range required {
		field, ok := fields[name]
		if !ok || bytes.Equal(field, []byte("null")) {
			return fmt.Errorf("%s is required", name)
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	return decoder.Decode(value)
}

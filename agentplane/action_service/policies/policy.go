// Package policies holds the Action Service's auto-approve policies: a PolicySet's `action_auto_approve` rules, each
// one kind's test of the requests for the Actions it lists, and the decision provider that walks a caller's bindings
// through them.
//
// A kind is one typed evaluator: the rule carries `type` and parameters, and this package owns the semantics. A
// constraint the rule cannot express is a new kind, never a DSL.
package policies

import (
	"context"
	"errors"
	"fmt"

	"k8s.io/apimachinery/pkg/util/sets"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// RuleType is the type of the PolicySet rules this package reads.
const RuleType = "action_auto_approve"

// Policy is an `action_auto_approve` rule's policy: one kind's test, bounded to the Actions it lists.
type Policy struct {
	// Actions holds the Action names the policy lists, by ActionGroup key.
	Actions map[string]sets.Set[string]
	Kind    Kind
}

// Kind is one policy kind: its parameters and its test of a request for a listed Action.
type Kind interface {
	Type() store.PolicyKind
	evaluate(ctx context.Context, action store.ActionIdentity, arguments map[string]any, visibility *Visibility) Outcome
}

// Outcome is a policy's answer to one request: Matched or NotMatched.
type Outcome interface{ isOutcome() }

type Matched struct {
	Explanation string
	// Repository is the repository a GitHub kind matched.
	Repository *store.MatchedRepository
}

// NotMatched sends the request to the human path; a policy never denies.
type NotMatched struct{ Reason string }

func (Matched) isOutcome()    {}
func (NotMatched) isOutcome() {}

// ParseRule reads an `action_auto_approve` rule. The PolicySet CRD has already pruned unknown fields and held each
// policy type to its own fields; this checks what the CRD cannot say.
func ParseRule(rule policyapi.Rule) (Policy, error) {
	spec := rule.Policy
	if spec == nil {
		return Policy{}, errors.New("an action_auto_approve rule needs a policy")
	}
	actions, err := parseActions(spec.Actions)
	if err != nil {
		return Policy{}, err
	}
	kind, err := parseKind(*spec, actions)
	if err != nil {
		return Policy{}, err
	}
	return Policy{Actions: actions, Kind: kind}, nil
}

func parseActions(listed map[string]policyapi.ActionNames) (map[string]sets.Set[string], error) {
	if len(listed) == 0 {
		return nil, errors.New("actions lists no ActionGroup")
	}
	actions := make(map[string]sets.Set[string], len(listed))
	for group, names := range listed {
		if len(names) == 0 {
			return nil, fmt.Errorf("group %q lists no actions", group)
		}
		for _, name := range names {
			if err := (store.ActionIdentity{Group: group, Name: name}).Validate(); err != nil {
				return nil, err
			}
		}
		actions[group] = sets.New(names...)
	}
	return actions, nil
}

func parseKind(spec policyapi.ActionPolicy, actions map[string]sets.Set[string]) (Kind, error) {
	switch store.PolicyKind(spec.Type) {
	case store.PolicyExactActions:
		return ExactActions{}, nil
	case store.PolicyArgumentSchema:
		return parseArgumentSchema(spec)
	case store.PolicyGitHubRepository:
		return GitHubRepository{Owner: spec.Owner, Repository: spec.Repository}, nil
	case store.PolicyGitHubPublicRepository:
		return GitHubPublicRepository{}, nil
	case store.PolicyHomeAssistantEntityControl:
		return parseHomeAssistantEntityControl(spec, actions)
	default:
		return nil, fmt.Errorf("unknown policy type %q", spec.Type)
	}
}

func (p Policy) lists(action store.ActionIdentity) bool {
	return p.Actions[action.Group].Has(action.Name)
}

// evaluate tests one request: the Action must be listed, and the kind's own test must pass.
func (p Policy) evaluate(ctx context.Context, action store.ActionIdentity, arguments map[string]any, visibility *Visibility) Outcome {
	if !p.lists(action) {
		return NotMatched{Reason: fmt.Sprintf("%s/%s is not listed", action.Group, action.Name)}
	}
	return p.Kind.evaluate(ctx, action, arguments, visibility)
}

// ExactActions matches a listed Action by name alone.
type ExactActions struct{}

func (ExactActions) Type() store.PolicyKind { return store.PolicyExactActions }

func (ExactActions) evaluate(_ context.Context, action store.ActionIdentity, _ map[string]any, _ *Visibility) Outcome {
	return Matched{Explanation: fmt.Sprintf("exact action %s/%s is listed", action.Group, action.Name)}
}

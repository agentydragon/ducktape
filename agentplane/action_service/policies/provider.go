package policies

import (
	"context"
	"fmt"
	"strings"
	"unicode/utf8"

	"k8s.io/apimachinery/pkg/util/sets"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
)

const (
	ProviderName      = "action_policy_set"
	AutoApproveReason = "policy_set_auto_approve"
	// descriptionLimit is the stored bound on a reason description, in code points; object names alone can approach
	// it.
	descriptionLimit = 500
)

// Binding is one PolicyBinding naming the caller, with the valid sets it names that exist, as the policy index held
// them at admission. A later edit, expiry or deletion changes the next request's Decision, not this one's.
type Binding struct {
	Namespace       string
	Name            string
	ResourceVersion string
	PolicySets      []PolicySet
}

// PolicySet is one valid set a binding names, with its auto-approve policies in evaluation order, each numbered by its
// position among all of the set's rules.
type PolicySet struct {
	Namespace  string
	Name       string
	Generation int64
	Policies   []reader.NumberedRule[Policy]
}

// Decision is the provider's answer to one request: Allow or NoOpinion.
type Decision interface{ isDecision() }

// Allow carries the vote that auto-approves the request.
type Allow struct{ Vote store.ProviderVote }

// NoOpinion leaves the request to a human. ReasonDescription says why each policy listing the Action did not match, or
// that none lists it.
type NoOpinion struct{ ReasonDescription string }

func (Allow) isDecision()     {}
func (NoOpinion) isDecision() {}

// Provider allows a request the caller's bindings auto-approve and has no opinion on any other. An auto-approve rule is
// the only form an Action policy takes; there is no deny form, so what does not match waits for a human.
type Provider struct {
	visibility *Visibility
}

func NewProvider(visibility *Visibility) *Provider {
	return &Provider{visibility: visibility}
}

// Decide walks the bindings, then each binding's sets, then each set's policies, and allows on the first match.
func (p *Provider) Decide(ctx context.Context, bindings []Binding, action store.ActionIdentity, arguments map[string]any) Decision {
	var declined []string
	for _, binding := range bindings {
		for _, set := range binding.PolicySets {
			for _, numbered := range set.Policies {
				policy := numbered.Rule
				switch outcome := policy.evaluate(ctx, action, arguments, p.visibility).(type) {
				case Matched:
					description := truncated(fmt.Sprintf("binding %s/%s set %s %s[%d] %s: %s",
						binding.Namespace, binding.Name, set.Name, store.PolicySourceRules, numbered.Number,
						policy.Kind.Type(), outcome.Explanation))
					return Allow{Vote: store.ProviderVote{
						Provider:          ProviderName,
						Verdict:           store.VerdictAllow,
						ReasonCode:        AutoApproveReason,
						ReasonDescription: &description,
						Evidence: evidence(bindings, store.MatchedPolicy{
							Namespace:  set.Namespace,
							PolicySet:  set.Name,
							Source:     store.PolicySourceRules,
							Index:      int64(numbered.Number),
							Type:       policy.Kind.Type(),
							Repository: outcome.Repository,
						}),
					}}
				case NotMatched:
					if policy.lists(action) {
						declined = append(declined, fmt.Sprintf("set %s %s[%d] %s: %s",
							set.Name, store.PolicySourceRules, numbered.Number, policy.Kind.Type(), outcome.Reason))
					}
				}
			}
		}
	}
	if len(declined) == 0 {
		return NoOpinion{ReasonDescription: truncated(fmt.Sprintf(
			"no auto-approve policy bound to the caller lists %s/%s", action.Group, action.Name,
		))}
	}
	return NoOpinion{ReasonDescription: truncated(strings.Join(declined, "; "))}
}

// evidence is every binding the request was evaluated against and every set they name, each set once.
func evidence(bindings []Binding, matched store.MatchedPolicy) *store.PolicyEvidence {
	recorded := &store.PolicyEvidence{Matched: matched}
	seen := sets.New[[2]string]()
	for _, binding := range bindings {
		recorded.Bindings = append(recorded.Bindings, store.BindingEvidence{
			Namespace: binding.Namespace, Name: binding.Name, ResourceVersion: binding.ResourceVersion,
		})
		for _, set := range binding.PolicySets {
			if key := [2]string{set.Namespace, set.Name}; !seen.Has(key) {
				seen.Insert(key)
				recorded.PolicySets = append(recorded.PolicySets, store.PolicySetEvidence{
					Namespace: set.Namespace, Name: set.Name, Generation: set.Generation,
				})
			}
		}
	}
	return recorded
}

func truncated(description string) string {
	if utf8.RuneCountInString(description) <= descriptionLimit {
		return description
	}
	return string([]rune(description)[:descriptionLimit])
}

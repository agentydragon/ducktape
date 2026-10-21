package policy

import (
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// EffectivePolicy is an egress policy some binding grants the caller.
type EffectivePolicy struct {
	Name  string
	Rules []EffectiveRule
}

// EffectiveRule is a rule of an EffectivePolicy and the credential it presents.
type EffectiveRule struct {
	Rule resources.Rule
	// Credential is nil when the rule names none, or names one the index does not hold: nothing to
	// present, so nothing to substitute, which is also what Evaluate does with it.
	Credential *resources.Credential
}

// ListEffectivePolicies returns the egress policies the caller holds, as Evaluate would walk them:
// bindings by name, each binding's sets as it lists them. A set granted by two bindings appears
// under each.
func (i *Index) ListEffectivePolicies(caller policyapi.ServiceAccountRef) []EffectivePolicy {
	grants := i.Rules.Grants(caller)
	i.mu.RLock()
	defer i.mu.RUnlock()
	var policies []EffectivePolicy
	for _, granted := range grants {
		for _, set := range granted.Sets {
			policy := EffectivePolicy{Name: set.Name}
			for _, numbered := range set.Rules {
				effective := EffectiveRule{Rule: numbered.Rule}
				if ref := numbered.Rule.CredentialRef; ref != nil {
					if credential, ok := i.credentials[*ref]; ok {
						effective.Credential = &credential
					}
				}
				policy.Rules = append(policy.Rules, effective)
			}
			policies = append(policies, policy)
		}
	}
	return policies
}

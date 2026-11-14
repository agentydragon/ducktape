// Package environmentaccess parses and evaluates environment lifecycle rules.
package environmentaccess

import (
	"errors"
	"fmt"
	"slices"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

const (
	ruleType    = "environment"
	resume      = "resume"
	bindSession = "bind_session"
)

// Ready is the condition kept on bindings whose environment rules this reader enforces.
var Ready = reader.Condition{
	Type: policyapi.EnvironmentReady, FieldManager: "agentplane-runner-gateway", Rules: "environment rules",
	Reason: "Enforced",
}

// Rule is an environment rule this package can enforce. Its selectors are evaluated against the
// current Sandbox on every lookup, so boundIdentity:self follows the caller's Identity.
type Rule struct {
	Operations   []string
	Environments []policyapi.EnvironmentSelector
}

// NewIndex returns the shared Identity grant index for environment rules.
func NewIndex() *reader.Index[Rule] {
	return reader.NewIndex(ruleType, ParseRule)
}

// ParseRule reads one environment rule, refusing operations or fields this slice cannot enforce.
func ParseRule(rule policyapi.Rule) (Rule, error) {
	if rule.Type != ruleType {
		return Rule{}, fmt.Errorf("rule type %q is not %q", rule.Type, ruleType)
	}
	if len(rule.Operations) == 0 || len(rule.Operations) > 2 {
		return Rule{}, errors.New("operations must contain resume, bind_session, or both")
	}
	seen := map[string]bool{}
	for _, operation := range rule.Operations {
		if (operation != resume && operation != bindSession) || seen[operation] {
			return Rule{}, fmt.Errorf("unsupported or duplicate operation %q", operation)
		}
		seen[operation] = true
	}
	if len(rule.Environments) == 0 {
		return Rule{}, errors.New("environments must name at least one selector")
	}
	if rule.Role != nil || rule.Namespaces != nil || rule.ClusterRole != "" || rule.Hosts != nil ||
		rule.Methods != nil || rule.Paths != nil || rule.CredentialRef != nil || rule.ClusterInternal ||
		rule.Policy != nil || rule.Targets != nil {
		return Rule{}, errors.New("fields are not valid for an environment rule")
	}
	for i, selector := range rule.Environments {
		if selector.Sandbox == nil && selector.BoundIdentity == nil {
			return Rule{}, fmt.Errorf("environments[%d] must name a Sandbox or a bound Identity", i)
		}
		if seen[bindSession] && selector.Sandbox != nil && selector.BoundIdentity == nil {
			return Rule{}, fmt.Errorf("environments[%d].sandbox must also name boundIdentity for bind_session", i)
		}
		if selector.Sandbox != nil && selector.Sandbox.Name == "" {
			return Rule{}, fmt.Errorf("environments[%d].sandbox.name is required", i)
		}
		if selector.BoundIdentity != nil {
			identity := selector.BoundIdentity
			if (identity.Name != "") == identity.Self {
				return Rule{}, fmt.Errorf("environments[%d].boundIdentity must set exactly one of name or self", i)
			}
		}
	}
	return Rule{Operations: rule.Operations, Environments: rule.Environments}, nil
}

// Allowed reports whether the caller's current Identity grants operation on this Sandbox.
// Callers check policy readiness and live target/Identity facts separately.
func Allowed(index *reader.Index[Rule], caller reader.Identity, operation string, sandbox *sandboxapi.Sandbox) bool {
	if index == nil || sandbox == nil || caller.Name == "" || caller.UID == "" || (operation != resume && operation != bindSession) {
		return false
	}
	for _, grant := range index.Grants(caller) {
		for _, set := range grant.Sets {
			for _, numbered := range set.Rules {
				if !slices.Contains(numbered.Rule.Operations, operation) {
					continue
				}
				for _, selector := range numbered.Rule.Environments {
					if matches(selector, caller.Name, sandbox) {
						return true
					}
				}
			}
		}
	}
	return false
}

func matches(selector policyapi.EnvironmentSelector, caller string, sandbox *sandboxapi.Sandbox) bool {
	if selector.Sandbox != nil {
		target := selector.Sandbox
		if sandbox.Name != target.Name || (target.UID != "" && target.UID != sandbox.UID) {
			return false
		}
	}
	if selector.BoundIdentity != nil {
		if sandbox.Spec.Identity == nil {
			return false
		}
		identity := selector.BoundIdentity.Name
		if selector.BoundIdentity.Self {
			identity = caller
		}
		if sandbox.Spec.Identity.Name != identity {
			return false
		}
	}
	return true
}

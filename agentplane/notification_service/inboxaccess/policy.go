// Package inboxaccess enforces the `session_inbox_access` rules of PolicySets: which Sandboxes' Session inboxes a
// ServiceAccount may send messages to, by the PolicyBindings that grant it those rules.
package inboxaccess

import (
	"errors"
	"fmt"
	"slices"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"k8s.io/client-go/rest"
)

const (
	ruleType = "session_inbox_access"
	// sendMessage is the one operation a rule can allow so far.
	sendMessage = "send_message"
)

// Ready is the condition this service keeps on each PolicyBinding.
var Ready = reader.Condition{
	Type: "SessionInboxAccessReady", FieldManager: "agentplane-notifications", Rules: "session inbox access rules",
	Reason: "Enforced",
}

// Rule is a `session_inbox_access` rule that allows `send_message` to its targets.
type Rule struct{ Targets policyapi.SandboxTargets }

// ParseRule reads a rule, refusing one this service cannot enforce as written.
func ParseRule(rule policyapi.Rule) (Rule, error) {
	if len(rule.Operations) == 0 {
		return Rule{}, errors.New("operations is required")
	}
	for _, operation := range rule.Operations {
		if operation != sendMessage {
			return Rule{}, fmt.Errorf("operation %q is not one this service enforces", operation)
		}
	}
	if rule.Targets == nil || (len(rule.Targets.Sandboxes) == 0 && len(rule.Targets.InboxTags) == 0 && !rule.Targets.AllSandboxes) {
		return Rule{}, errors.New("targets must name a Sandbox or an inbox tag, or set allSandboxes")
	}
	return Rule{Targets: *rule.Targets}, nil
}

// covers reports whether the rule targets the Sandbox as it is now: every Sandbox, this one by name with no UID or
// with its UID, or an inbox tag it holds.
func (r Rule) covers(sandbox *sandboxapi.Sandbox) bool {
	if r.Targets.AllSandboxes {
		return true
	}
	for _, target := range r.Targets.Sandboxes {
		if target.Name == sandbox.Name && (target.UID == "" || target.UID == sandbox.UID) {
			return true
		}
	}
	return slices.ContainsFunc(r.Targets.InboxTags, func(tag string) bool { return slices.Contains(sandbox.Spec.InboxTags, tag) })
}

// Policy is this service's view of the send rules of its namespace.
type Policy struct {
	Rules  *reader.Index[Rule]
	Health *watchhealth.Health
}

// NewPolicy returns an empty Policy that is unavailable until Watch fills it, and while a watched kind fails to list
// or watch.
func NewPolicy() *Policy {
	return &Policy{Rules: reader.NewIndex(ruleType, ParseRule), Health: watchhealth.New()}
}

// Watch returns the watches that keep the policy equal to the namespace's PolicySets and PolicyBindings, and Ready
// on each binding, from Run on.
func (p *Policy) Watch(base *rest.Config, namespace string) (*reader.Watches, error) {
	watches := reader.NewWatches(base, p.Health)
	return watches, reader.WatchRules(watches, p.Rules, namespace, Ready)
}

// PermitsSend reports whether a binding of the sender grants a rule that allows sending to the Sandbox as the caller
// read it. It says nothing of availability, which the caller checks first.
func (p *Policy) PermitsSend(sender policyapi.ServiceAccountRef, sandbox *sandboxapi.Sandbox) bool {
	for _, grant := range p.Rules.Grants(sender) {
		for _, set := range grant.Sets {
			for _, rule := range set.Rules {
				if rule.Rule.covers(sandbox) {
					return true
				}
			}
		}
	}
	return false
}

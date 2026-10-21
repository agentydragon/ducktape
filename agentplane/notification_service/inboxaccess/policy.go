// Package inboxaccess enforces the `session_inbox_access` rules of PolicySets: which Sandboxes' Session inboxes a
// ServiceAccount may send messages to, by the PolicyBindings that grant it those rules.
package inboxaccess

import (
	"errors"
	"fmt"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"k8s.io/apimachinery/pkg/types"
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
	if rule.Targets == nil || (len(rule.Targets.Sandboxes) == 0 && !rule.Targets.AllSandboxes) {
		return Rule{}, errors.New("targets must name a Sandbox or set allSandboxes")
	}
	return Rule{Targets: *rule.Targets}, nil
}

// covers reports whether the rule targets the Sandbox incarnation: every Sandbox, or this one by name with no UID
// or with its UID.
func (r Rule) covers(name string, uid types.UID) bool {
	if r.Targets.AllSandboxes {
		return true
	}
	for _, target := range r.Targets.Sandboxes {
		if target.Name == name && (target.UID == "" || target.UID == uid) {
			return true
		}
	}
	return false
}

// Policy is this service's view of the send rules of its namespace.
type Policy struct {
	Rules     *reader.Index[Rule]
	Freshness *reader.Freshness
}

// NewPolicy returns an empty Policy that is unavailable until Watch fills it, and once a watched kind's last
// completed cycle is older than staleAfter.
func NewPolicy(staleAfter time.Duration) *Policy {
	return &Policy{
		Rules:     reader.NewIndex(ruleType, ParseRule),
		Freshness: reader.NewFreshness(staleAfter, reader.PolicySets, reader.PolicyBindings),
	}
}

// Watch returns the watches that keep the policy equal to the namespace's PolicySets and PolicyBindings, and Ready
// on each binding, from Run on. Its freshness should be reader.StaleAfterCycles of period.
func (p *Policy) Watch(base *rest.Config, namespace string, period time.Duration) (*reader.Watches, error) {
	watches, err := reader.NewWatches(base, p.Freshness, period)
	if err != nil {
		return nil, err
	}
	return watches, reader.WatchRules(watches, p.Rules, namespace, Ready)
}

// PermitsSend reports whether a binding of the sender grants a rule that allows sending to the Sandbox
// incarnation. It says nothing of freshness, which the caller checks first.
func (p *Policy) PermitsSend(sender policyapi.ServiceAccountRef, name string, uid types.UID) bool {
	for _, grant := range p.Rules.Grants(sender) {
		for _, set := range grant.Sets {
			for _, rule := range set.Rules {
				if rule.Rule.covers(name, uid) {
					return true
				}
			}
		}
	}
	return false
}

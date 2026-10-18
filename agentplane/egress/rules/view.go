package rules

import (
	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
)

// The view types are what a sandbox may know about its own egress. They are built field by field
// from policy.EffectivePolicy and never from a resource object, so a field added to an
// EgressCredential, a second source say, reaches a sandbox only when someone writes it in here.

// EgressView is everything the sandbox may reach, as the proxy would decide it right now.
type EgressView struct {
	Subject  policyapi.ServiceAccountRef `json:"subject" jsonschema_description:"The ServiceAccount this view is of."`
	Policies []PolicyView                `json:"policies" jsonschema_description:"Granted egress policies; empty means no egress."`
}

// PolicyView is a granted egress policy.
type PolicyView struct {
	Name  string     `json:"name"`
	Rules []RuleView `json:"rules"`
}

// RuleView is one rule as the sandbox may see it.
type RuleView struct {
	Hosts      []string        `json:"hosts"`
	Methods    []string        `json:"methods" jsonschema:"nullable" jsonschema_description:"Null admits any method."`
	Paths      []string        `json:"paths" jsonschema:"nullable" jsonschema_description:"Null admits any path."`
	Credential *CredentialView `json:"credential" jsonschema:"nullable" jsonschema_description:"Present when the proxy substitutes one for this rule."`
}

// CredentialView is how to present a credential the sandbox does not hold: what to send, and where.
type CredentialView struct {
	Name        string `json:"name"`
	Description string `json:"description" jsonschema_description:"What the credential is and what it can do, as its owner wrote it."`
	Placeholder string `json:"placeholder" jsonschema_description:"Inert string to send; the proxy swaps the real value in."`
	// Targets is presentation syntax only: egressapi.Target carries no value and no source.
	Targets []egressapi.Target `json:"targets" jsonschema_description:"Every location the proxy substitutes at. The method selects the fields a target carries: header (wholeValue, schemeToken, basicUsername, basicPassword, basicWhole), scheme (schemeToken), field (jsonField)."`
}

// ViewOf is subject's view of the egress policies it holds.
func ViewOf(subject policyapi.ServiceAccountRef, policies []policy.EffectivePolicy) EgressView {
	view := EgressView{Subject: subject, Policies: []PolicyView{}}
	for _, effective := range policies {
		policyView := PolicyView{Name: effective.Name, Rules: []RuleView{}}
		for _, rule := range effective.Rules {
			policyView.Rules = append(policyView.Rules, ruleView(rule))
		}
		view.Policies = append(view.Policies, policyView)
	}
	return view
}

func ruleView(effective policy.EffectiveRule) RuleView {
	view := RuleView{Hosts: effective.Rule.Hosts, Methods: effective.Rule.Methods}
	if effective.Rule.Paths != nil {
		view.Paths = make([]string, len(effective.Rule.Paths))
		for i, glob := range effective.Rule.Paths {
			view.Paths[i] = glob.Pattern
		}
	}
	if credential := effective.Credential; credential != nil {
		view.Credential = &CredentialView{
			Name: credential.Name, Description: credential.Description, Placeholder: credential.Placeholder(),
		}
		for _, target := range credential.Targets {
			view.Credential.Targets = append(view.Credential.Targets, target.ToAPI())
		}
	}
	return view
}

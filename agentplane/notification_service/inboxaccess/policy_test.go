package inboxaccess

import (
	"testing"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const (
	testNamespace = "test-namespace"
	currentUID    = types.UID("00000000-0000-4000-8000-000000000001")
	formerUID     = types.UID("00000000-0000-4000-8000-000000000002")
)

var (
	sender = policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "test-sender"}
	other  = policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "test-other"}
)

// recipient is the current incarnation of the test recipient, holding the inbox tags.
func recipient(tags ...string) *sandboxapi.Sandbox {
	return &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "test-recipient", UID: currentUID},
		Spec:       sandboxapi.SandboxSpec{InboxTags: tags},
	}
}

func sendRule(targets policyapi.SandboxTargets) policyapi.Rule {
	return policyapi.Rule{Type: ruleType, Operations: []string{sendMessage}, Targets: &targets}
}

// policyOf is a Policy holding one set of the rules, bound to sender.
func policyOf(rules ...policyapi.Rule) *Policy {
	policy := NewPolicy()
	policy.Rules.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "test-set"},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	})
	policy.Rules.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "test-binding"},
		Spec:       policyapi.PolicyBindingSpec{Subject: sender, PolicySets: []string{"test-set"}},
	})
	return policy
}

func TestASendIsPermittedOnlyToTheSandboxesARuleTargets(t *testing.T) {
	named := func(target policyapi.SandboxTarget) policyapi.SandboxTargets {
		return policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{target}}
	}
	cases := []struct {
		name    string
		targets policyapi.SandboxTargets
		sandbox *sandboxapi.Sandbox
		want    bool
	}{
		{"named without a UID", named(policyapi.SandboxTarget{Name: "test-recipient"}), recipient(), true},
		{"named with its UID", named(policyapi.SandboxTarget{Name: "test-recipient", UID: currentUID}), recipient(), true},
		{"named with another incarnation's UID", named(policyapi.SandboxTarget{Name: "test-recipient", UID: formerUID}), recipient(), false},
		{"another Sandbox named", named(policyapi.SandboxTarget{Name: "test-elsewhere"}), recipient(), false},
		{"every Sandbox", policyapi.SandboxTargets{AllSandboxes: true}, recipient(), true},
		{"a tag it holds", policyapi.SandboxTargets{InboxTags: []string{"test-other-group", "test-group"}}, recipient("test-group"), true},
		{"a tag it does not hold", policyapi.SandboxTargets{InboxTags: []string{"test-other-group"}}, recipient("test-group"), false},
		{"a tag, to an untagged Sandbox", policyapi.SandboxTargets{InboxTags: []string{"test-group"}}, recipient(), false},
		{
			"a tag it no longer holds beside its name",
			policyapi.SandboxTargets{InboxTags: []string{"test-group"}, Sandboxes: []policyapi.SandboxTarget{{Name: "test-recipient"}}},
			recipient(), true,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := policyOf(sendRule(c.targets)).PermitsSend(sender, c.sandbox); got != c.want {
				t.Errorf("PermitsSend = %v, want %v", got, c.want)
			}
		})
	}
}

func TestOnlyTheBoundAccountMaySend(t *testing.T) {
	policy := policyOf(sendRule(policyapi.SandboxTargets{AllSandboxes: true}))
	if policy.PermitsSend(other, recipient()) {
		t.Error("an account no binding names may send")
	}
	policy.Rules.DeleteBinding("test-binding")
	if policy.PermitsSend(sender, recipient()) {
		t.Error("the account may still send once its binding is gone")
	}
}

func TestOtherReadersRulesGrantNoSend(t *testing.T) {
	policy := policyOf(policyapi.Rule{Type: "egress", Hosts: []string{"inbox-access-test.invalid"}})
	if policy.PermitsSend(sender, recipient()) {
		t.Error("an egress rule allows a send")
	}
}

func TestARuleThisServiceCannotEnforceIsRefused(t *testing.T) {
	for name, rule := range map[string]policyapi.Rule{
		"no operation":      {Type: ruleType, Targets: &policyapi.SandboxTargets{AllSandboxes: true}},
		"unknown operation": {Type: ruleType, Operations: []string{"read_transcript"}, Targets: &policyapi.SandboxTargets{AllSandboxes: true}},
		"no targets":        {Type: ruleType, Operations: []string{sendMessage}},
		"empty targets":     {Type: ruleType, Operations: []string{sendMessage}, Targets: &policyapi.SandboxTargets{}},
	} {
		if _, err := ParseRule(rule); err == nil {
			t.Errorf("%s: parsed", name)
		}
	}
}

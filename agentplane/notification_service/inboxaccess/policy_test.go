package inboxaccess

import (
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
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

func sendRule(targets policyapi.SandboxTargets) policyapi.Rule {
	return policyapi.Rule{Type: ruleType, Operations: []string{sendMessage}, Targets: &targets}
}

// policyOf is a Policy holding one set of the rules, bound to sender.
func policyOf(rules ...policyapi.Rule) *Policy {
	policy := NewPolicy(time.Minute)
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

func TestASendIsPermittedOnlyToTheIncarnationsARuleTargets(t *testing.T) {
	cases := []struct {
		name    string
		targets policyapi.SandboxTargets
		sandbox string
		uid     types.UID
		want    bool
	}{
		{"named without a UID, any incarnation", policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{{Name: "test-recipient"}}}, "test-recipient", formerUID, true},
		{"named with its UID", policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{{Name: "test-recipient", UID: currentUID}}}, "test-recipient", currentUID, true},
		{"named with another incarnation's UID", policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{{Name: "test-recipient", UID: formerUID}}}, "test-recipient", currentUID, false},
		{"another Sandbox named", policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{{Name: "test-elsewhere"}}}, "test-recipient", currentUID, false},
		{"every Sandbox", policyapi.SandboxTargets{AllSandboxes: true}, "test-recipient", currentUID, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := policyOf(sendRule(c.targets)).PermitsSend(sender, c.sandbox, c.uid); got != c.want {
				t.Errorf("PermitsSend = %v, want %v", got, c.want)
			}
		})
	}
}

func TestOnlyTheBoundAccountMaySend(t *testing.T) {
	policy := policyOf(sendRule(policyapi.SandboxTargets{AllSandboxes: true}))
	if policy.PermitsSend(other, "test-recipient", currentUID) {
		t.Error("an account no binding names may send")
	}
	policy.Rules.DeleteBinding("test-binding")
	if policy.PermitsSend(sender, "test-recipient", currentUID) {
		t.Error("the account may still send once its binding is gone")
	}
}

func TestOtherReadersRulesGrantNoSend(t *testing.T) {
	policy := policyOf(policyapi.Rule{Type: "egress", Hosts: []string{"inbox-access-test.invalid"}})
	if policy.PermitsSend(sender, "test-recipient", currentUID) {
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

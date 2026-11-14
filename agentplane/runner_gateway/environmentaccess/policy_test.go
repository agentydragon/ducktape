package environmentaccess

import (
	"testing"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const (
	testCaller = "test-caller"
	testUID    = types.UID("00000000-0000-4000-8000-000000000001")
	testTarget = types.UID("00000000-0000-4000-8000-000000000002")
	otherUID   = types.UID("00000000-0000-4000-8000-000000000003")
)

func TestAllowedMatchesEachSelectorAsAnAndAndTheListAsAnOr(t *testing.T) {
	index := indexFor(t, policyapi.EnvironmentSelector{
		Sandbox:       &policyapi.SandboxTarget{Name: "test-first", UID: otherUID},
		BoundIdentity: &policyapi.BoundIdentitySelector{Name: "test-worker"},
	}, policyapi.EnvironmentSelector{
		Sandbox:       &policyapi.SandboxTarget{Name: "test-target", UID: testTarget},
		BoundIdentity: &policyapi.BoundIdentitySelector{Name: "test-worker"},
	})
	target := sandbox("test-target", testTarget, "test-worker")
	if !Allowed(index, caller(), resume, target) {
		t.Fatal("a Sandbox matching both fields of one selector was refused")
	}

	for name, sandbox := range map[string]*sandboxapi.Sandbox{
		"wrong incarnation":      sandbox("test-target", otherUID, "test-worker"),
		"wrong bound Identity":   sandbox("test-target", testTarget, "test-other"),
		"missing bound Identity": sandbox("test-target", testTarget, ""),
		"wrong Sandbox name":     sandbox("test-other", testTarget, "test-worker"),
	} {
		t.Run(name, func(t *testing.T) {
			if Allowed(index, caller(), resume, sandbox) {
				t.Error("a Sandbox that did not match every selector field was allowed")
			}
		})
	}
}

func TestSelfSelectorResolvesAgainstTheLookupIdentity(t *testing.T) {
	index := indexFor(t, policyapi.EnvironmentSelector{
		BoundIdentity: &policyapi.BoundIdentitySelector{Self: true},
	})
	if !Allowed(index, caller(), resume, sandbox("test-target", testTarget, testCaller)) {
		t.Fatal("a Sandbox bound to the caller did not match boundIdentity:self")
	}
	if Allowed(index, caller(), resume, sandbox("test-target", testTarget, "test-other")) {
		t.Fatal("boundIdentity:self matched an Identity other than the caller")
	}
}

func TestSandboxNameWithoutAUIDMatchesItsCurrentIncarnation(t *testing.T) {
	index := indexFor(t, policyapi.EnvironmentSelector{
		Sandbox: &policyapi.SandboxTarget{Name: "test-target"},
	})
	if !Allowed(index, caller(), resume, sandbox("test-target", testTarget, "test-worker")) {
		t.Fatal("a name selector did not match the Sandbox's current incarnation")
	}
}

func TestAllowedRequiresAValidSupportedOperationAndIdentityCaller(t *testing.T) {
	index := indexFor(t, policyapi.EnvironmentSelector{Sandbox: &policyapi.SandboxTarget{Name: "test-target"}})
	target := sandbox("test-target", testTarget, "test-worker")
	for name, args := range map[string]struct {
		caller    reader.Identity
		operation string
		sandbox   *sandboxapi.Sandbox
	}{
		"unsupported operation":  {caller: caller(), operation: "bind_session", sandbox: target},
		"caller without a grant": {caller: reader.Identity{Name: "test-other", UID: otherUID}, operation: resume, sandbox: target},
		"empty caller name":      {caller: reader.Identity{UID: testUID}, operation: resume, sandbox: target},
		"empty caller UID":       {caller: reader.Identity{Name: testCaller}, operation: resume, sandbox: target},
		"nil Sandbox":            {caller: caller(), operation: resume},
	} {
		t.Run(name, func(t *testing.T) {
			if Allowed(index, args.caller, args.operation, args.sandbox) {
				t.Error("invalid caller or operation was allowed")
			}
		})
	}
}

func TestParseRuleRefusesOperationsFieldsAndMalformedSelectors(t *testing.T) {
	valid := rule(policyapi.EnvironmentSelector{Sandbox: &policyapi.SandboxTarget{Name: "test-target"}})
	invalid := map[string]policyapi.Rule{
		"wrong type":            {Type: "session_inbox_access", Operations: []string{resume}, Environments: valid.Environments},
		"missing operation":     {Type: ruleType, Environments: valid.Environments},
		"unsupported operation": ruleWithOperations([]string{"bind_session"}, valid.Environments...),
		"duplicate operation":   ruleWithOperations([]string{resume, resume}, valid.Environments...),
		"missing selectors":     {Type: ruleType, Operations: []string{resume}},
		"empty selector":        rule(policyapi.EnvironmentSelector{}),
		"empty Sandbox name":    rule(policyapi.EnvironmentSelector{Sandbox: &policyapi.SandboxTarget{}}),
		"empty bound Identity":  rule(policyapi.EnvironmentSelector{BoundIdentity: &policyapi.BoundIdentitySelector{}}),
		"name and self":         rule(policyapi.EnvironmentSelector{BoundIdentity: &policyapi.BoundIdentitySelector{Name: "test-worker", Self: true}}),
		"irrelevant field":      func() policyapi.Rule { r := valid; r.Targets = &policyapi.SandboxTargets{AllSandboxes: true}; return r }(),
	}
	if _, err := ParseRule(valid); err != nil {
		t.Fatalf("ParseRule(valid) error = %v", err)
	}
	for name, candidate := range invalid {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseRule(candidate); err == nil {
				t.Error("ParseRule accepted malformed or unsupported rule")
			}
		})
	}
}

func TestUnsupportedRulesAreRefusedByTheSharedReaderAndGrantNothing(t *testing.T) {
	index := NewIndex()
	index.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "test-set"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{
			rule(policyapi.EnvironmentSelector{Sandbox: &policyapi.SandboxTarget{Name: "test-target"}}),
			ruleWithOperations([]string{"bind_session"}, policyapi.EnvironmentSelector{Sandbox: &policyapi.SandboxTarget{Name: "test-target"}}),
		}},
	})
	index.PutIdentity(&policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Name: testCaller, UID: testUID}})
	index.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "test-binding"},
		Spec:       policyapi.PolicyBindingSpec{Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: testCaller}}, PolicySets: []string{"test-set"}},
	})

	resolution := index.Resolve([]string{"test-set"})
	if len(resolution.Refused) != 1 {
		t.Fatalf("refused PolicySets = %v, want one refused set", resolution.Refused)
	}
	if Allowed(index, caller(), resume, sandbox("test-target", testTarget, "test-worker")) {
		t.Fatal("a PolicySet containing an unsupported rule granted its valid rule")
	}
}

func caller() reader.Identity { return reader.Identity{Name: testCaller, UID: testUID} }

func sandbox(name string, uid types.UID, identity string) *sandboxapi.Sandbox {
	result := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: name, UID: uid}}
	if identity != "" {
		result.Spec.Identity = &sandboxapi.IdentityRef{Name: identity}
	}
	return result
}

func indexFor(t *testing.T, selectors ...policyapi.EnvironmentSelector) *reader.Index[Rule] {
	t.Helper()
	index := NewIndex()
	index.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "test-set"},
		Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{rule(selectors...)}},
	})
	index.PutIdentity(&policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Name: testCaller, UID: testUID}})
	index.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "test-binding"},
		Spec:       policyapi.PolicyBindingSpec{Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: testCaller}}, PolicySets: []string{"test-set"}},
	})
	if got := index.Resolve([]string{"test-set"}); len(got.Refused) > 0 || got.Rules() != 1 {
		t.Fatalf("PolicySet did not parse: %+v", got)
	}
	return index
}

func rule(selectors ...policyapi.EnvironmentSelector) policyapi.Rule {
	return ruleWithOperations([]string{resume}, selectors...)
}

func ruleWithOperations(operations []string, selectors ...policyapi.EnvironmentSelector) policyapi.Rule {
	return policyapi.Rule{Type: ruleType, Operations: operations, Environments: selectors}
}

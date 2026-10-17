package policy

import (
	"slices"
	"testing"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
)

func TestListEffectivePolicies(t *testing.T) {
	stranger := policyapi.ServiceAccountRef{Namespace: namespace, Name: "stranger"}
	index := standardIndex(
		[]*policyapi.PolicySet{
			policySet("github", githubRule, publicRule),
			policySet("dangling", egress([]string{"x.example.com"}, nil, nil, "gone")),
			policySet("another-reader", policyapi.Rule{Type: "actions"}),
		},
		binding("a", "github", "missing", "another-reader"),
		binding("b", "github", "dangling"),
		bindingFor(stranger, "c", "dangling"),
	)

	policies := index.ListEffectivePolicies(caller)

	var names []string
	for _, policy := range policies {
		names = append(names, policy.Name)
	}
	if want := []string{"github", "github", "dangling"}; !slices.Equal(names, want) {
		t.Fatalf("policies = %v, want %v: bindings by name, a set under each binding granting it, no missing set or other reader's", names, want)
	}
	github, dangling := policies[0].Rules, policies[2].Rules
	if github[0].Credential == nil || github[0].Credential.Name != githubCredential.Name {
		t.Errorf("rule naming a held credential presents %v, want %s", github[0].Credential, githubCredential.Name)
	}
	if github[1].Credential != nil {
		t.Errorf("rule naming no credential presents %v", github[1].Credential)
	}
	if dangling[0].Credential != nil {
		t.Errorf("rule naming a credential the index lacks presents %v", dangling[0].Credential)
	}
	if got := index.ListEffectivePolicies(policyapi.ServiceAccountRef{Namespace: namespace, Name: "nobody"}); len(got) != 0 {
		t.Errorf("a subject no binding names holds %v", got)
	}
}

func TestABindingWhoseEverySetIsAbsentListsNoEffectivePolicies(t *testing.T) {
	index := standardIndex([]*policyapi.PolicySet{policySet("unrelated", githubRule)}, binding("a", "missing", "also-missing"))

	if got := index.ListEffectivePolicies(caller); len(got) != 0 {
		t.Errorf("a binding naming only absent sets lists %v", got)
	}
}

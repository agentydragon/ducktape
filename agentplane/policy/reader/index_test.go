package reader

import (
	"fmt"
	"slices"
	"testing"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestResolveSeparatesMissingRefusedAndGranting(t *testing.T) {
	index := NewIndex(testRuleType, parseHosts)
	index.PutPolicySet(testSet("test-web", 2))
	index.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "test-broken"},
		Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: testRuleType}}},
	})
	got := index.Resolve([]string{"test-web", "test-late", "test-broken"})
	if got.Rules != 2 || !slices.Equal(got.Missing, []string{"test-late"}) || len(got.Refused) != 1 || got.Refused[0].Set != "test-broken" {
		t.Errorf("Resolve = %+v", got)
	}

	index.PutPolicySet(testSet("test-broken", 1))
	index.PutPolicySet(testSet("test-late", 1))
	if got := index.Resolve([]string{"test-web", "test-late", "test-broken"}); got.Rules != 4 || got.Missing != nil || got.Refused != nil {
		t.Errorf("Resolve after the sets were fixed or added = %+v", got)
	}
	index.DeletePolicySet("test-late")
	if got := index.Resolve([]string{"test-late"}); !slices.Equal(got.Missing, []string{"test-late"}) {
		t.Errorf("Resolve after delete = %+v", got)
	}
}

func TestGrantsAreTheCallersBindingsWithRulesOfTheType(t *testing.T) {
	index := NewIndex(testRuleType, parseHosts)
	index.PutPolicySet(testSet("test-own", 1))
	// Another reader's rule sits beside this reader's, and keeps its position in the numbering.
	index.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "test-mixed"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{
			{Type: "kubernetes_cluster_role_binding", ClusterRole: "test-role"}, testRule(),
		}},
	})
	index.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "test-other-reader"},
		Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: "kubernetes_cluster_role_binding", ClusterRole: "test-role"}}},
	})
	other := policyapi.ServiceAccountRef{Namespace: subject.Namespace, Name: "test-other-workload"}
	for name, binding := range map[string]policyapi.PolicyBindingSpec{
		"test-b":           {Subject: subject, PolicySets: []string{"test-mixed", "test-missing", "test-own"}},
		"test-a":           {Subject: subject, PolicySets: []string{"test-own"}},
		"test-other-sets":  {Subject: subject, PolicySets: []string{"test-other-reader"}},
		"test-other-agent": {Subject: other, PolicySets: []string{"test-own"}},
	} {
		index.PutBinding(&policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Name: name}, Spec: binding})
	}

	var got []string
	for _, grant := range index.Grants(subject) {
		for _, set := range grant.Sets {
			for _, rule := range set.Rules {
				got = append(got, fmt.Sprintf("%s/%s/%d", grant.Binding, set.Name, rule.Number))
			}
		}
	}
	if want := []string{"test-a/test-own/0", "test-b/test-mixed/1", "test-b/test-own/0"}; !slices.Equal(got, want) {
		t.Errorf("Grants = %v, want %v", got, want)
	}
}

func testSet(name string, rules int) *policyapi.PolicySet {
	set := &policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Name: name}}
	for range rules {
		set.Spec.Rules = append(set.Spec.Rules, testRule())
	}
	return set
}

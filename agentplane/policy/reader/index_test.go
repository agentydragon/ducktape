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
		ObjectMeta: metav1.ObjectMeta{Name: "test-broken", Generation: 7},
		Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: testRuleType}}},
	})
	got := index.Resolve([]string{"test-web", "test-late", "test-broken"})
	if got.Rules() != 2 || !slices.Equal(got.Missing, []string{"test-late"}) || len(got.Refused) != 1 ||
		got.Refused[0].Set != "test-broken" || got.Refused[0].Generation != 7 {
		t.Errorf("Resolve = %+v", got)
	}

	index.PutPolicySet(testSet("test-broken", 1))
	index.PutPolicySet(testSet("test-late", 1))
	if got := index.Resolve([]string{"test-web", "test-late", "test-broken"}); got.Rules() != 4 || got.Missing != nil || got.Refused != nil {
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

func TestBindingsAreEveryBindingNamingTheSubjectWithEachSetsStanding(t *testing.T) {
	index := NewIndex(testRuleType, parseHosts)
	index.PutPolicySet(testSet("test-own", 1))
	index.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "test-other-reader", Generation: 3},
		Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: "kubernetes_cluster_role_binding", ClusterRole: "test-role"}}},
	})
	index.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "test-broken"},
		Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: testRuleType}}},
	})
	other := policyapi.ServiceAccountRef{Namespace: subject.Namespace, Name: "test-other-workload"}
	for name, binding := range map[string]policyapi.PolicyBindingSpec{
		"test-b":           {Subject: subject, PolicySets: []string{"test-broken", "test-own", "test-missing"}},
		"test-a":           {Subject: subject, PolicySets: []string{"test-other-reader"}},
		"test-other-agent": {Subject: other, PolicySets: []string{"test-own"}},
	} {
		index.PutBinding(&policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Name: name}, Spec: binding})
	}

	got := index.Bindings(subject)
	if len(got) != 2 || got[0].Binding.Name != "test-a" || got[1].Binding.Name != "test-b" {
		t.Fatalf("Bindings = %+v", got)
	}
	// A set of another reader's rules still stands, with what it grants this one: nothing.
	if sets := got[0].Sets; len(sets) != 1 || sets[0].Name != "test-other-reader" || sets[0].Generation != 3 || len(sets[0].Rules) != 0 {
		t.Errorf("a binding of another reader's set: %+v", got[0])
	}
	if b := got[1]; len(b.Sets) != 1 || b.Sets[0].Name != "test-own" || !slices.Equal(b.Missing, []string{"test-missing"}) ||
		len(b.Refused) != 1 || b.Refused[0].Set != "test-broken" {
		t.Errorf("a binding of a set that parses, one that is missing and one that is refused: %+v", b)
	}
	// What a reader gets is its own copy.
	got[1].Binding.Spec.PolicySets[0] = "test-changed"
	if again := index.Bindings(subject); again[1].Binding.Spec.PolicySets[0] != "test-broken" {
		t.Errorf("changing a returned binding changed the index: %+v", again[1].Binding)
	}
}

func testSet(name string, rules int) *policyapi.PolicySet {
	set := &policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Name: name}}
	for range rules {
		set.Spec.Rules = append(set.Spec.Rules, testRule())
	}
	return set
}

package policy

import (
	"maps"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestAvailability(t *testing.T) {
	now := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	staleAfter := 3 * time.Minute
	healthy := func() *Index {
		index := NewIndex(staleAfter)
		for _, kind := range watchedKinds {
			index.RecordCycle(kind, now)
		}
		index.MarkSynced()
		return index
	}

	for name, c := range map[string]struct {
		index func() *Index
		at    time.Time
		want  bool
	}{
		"synced and fresh":                       {healthy, now.Add(staleAfter), true},
		"a cycle older than the staleness bound": {healthy, now.Add(staleAfter + time.Second), false},
		"a clock that moved backwards":           {healthy, now.Add(-time.Second), false},
		"before the first sync": {
			func() *Index {
				index := healthy()
				index.synced = false
				return index
			}, now, false,
		},
		"a kind that never completed a cycle": {
			func() *Index {
				index := healthy()
				delete(index.cycles, Secrets)
				return index
			}, now, false,
		},
		"one wedged kind among fresh ones": {
			func() *Index {
				index := healthy()
				index.RecordCycle(Bindings, now.Add(-time.Hour))
				return index
			}, now, false,
		},
		"draining": {
			func() *Index {
				index := healthy()
				index.MarkDraining()
				return index
			}, now, false,
		},
	} {
		t.Run(name, func(t *testing.T) {
			index := c.index()
			if got := index.Available(c.at); got != c.want {
				t.Errorf("Available(%v) = %v, want %v", c.at, got, c.want)
			}
			if got := index.Status(c.at).Available; got != c.want {
				t.Errorf("Status(%v).Available = %v, want %v", c.at, got, c.want)
			}
		})
	}
}

func TestStatusReportsEachCycleAge(t *testing.T) {
	now := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	index := NewIndex(3 * time.Minute)
	index.RecordCycle(Secrets, now.Add(-time.Minute))
	index.RecordCycle(Bindings, now.Add(-2*time.Minute))
	got := index.Status(now)
	if got.StaleAfter != 3*time.Minute || got.Synced || got.Draining {
		t.Errorf("Status = %+v", got)
	}
	if want := map[Kind]time.Duration{Secrets: time.Minute, Bindings: 2 * time.Minute}; !maps.Equal(got.Ages, want) {
		t.Errorf("Ages = %v, want %v", got.Ages, want)
	}
}

func TestResolveSeparatesMissingRefusedAndGranting(t *testing.T) {
	index := NewIndex(time.Minute)
	index.PutPolicySet(egressSet("web", 2))
	index.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "broken"},
		Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: resources.EgressRuleType}}},
	})
	got := index.Resolve([]string{"web", "late", "broken"})
	if got.Rules != 2 || !slices.Equal(got.Missing, []string{"late"}) || len(got.Refused) != 1 || got.Refused[0].Set != "broken" {
		t.Errorf("Resolve = %+v", got)
	}

	index.PutPolicySet(egressSet("broken", 1))
	index.PutPolicySet(egressSet("late", 1))
	if got := index.Resolve([]string{"web", "late", "broken"}); got.Rules != 4 || got.Missing != nil || got.Refused != nil {
		t.Errorf("Resolve after the sets were fixed or added = %+v", got)
	}
	index.DeletePolicySet("late")
	if got := index.Resolve([]string{"late"}); !slices.Equal(got.Missing, []string{"late"}) {
		t.Errorf("Resolve after delete = %+v", got)
	}
}

func egressSet(name string, rules int) *policyapi.PolicySet {
	set := &policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Name: name}}
	for range rules {
		set.Spec.Rules = append(set.Spec.Rules, policyapi.Rule{Type: resources.EgressRuleType, Hosts: []string{"example.test"}})
	}
	return set
}

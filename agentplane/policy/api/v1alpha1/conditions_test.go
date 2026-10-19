package v1alpha1

import (
	"slices"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestPrepareConditionApply(t *testing.T) {
	transitioned := metav1.NewTime(time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC))
	binding := &PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: "rules", Name: "grant", Generation: 3},
		Status: Status{Conditions: []metav1.Condition{
			{Type: "KubernetesReady", Status: metav1.ConditionTrue, Reason: "Applied", ObservedGeneration: 3, LastTransitionTime: transitioned},
			{Type: "EgressReady", Status: metav1.ConditionTrue, Reason: "Enforced", Message: "egress rules: 1", ObservedGeneration: 3, LastTransitionTime: transitioned},
		}},
	}
	before := binding.DeepCopy()

	if patch := binding.PrepareConditionApply(metav1.Condition{Type: "EgressReady", Status: metav1.ConditionTrue, Reason: "Enforced", Message: "egress rules: 1"}); patch != nil {
		t.Errorf("a condition the binding already carries was applied again: %+v", patch)
	}

	condition := binding.PrepareConditionApply(metav1.Condition{Type: "EgressReady", Status: metav1.ConditionTrue, Reason: "Enforced", Message: "egress rules: 2"})
	if condition == nil {
		t.Fatal("changed condition was not returned")
	}
	if got := *condition; got.Type != "EgressReady" || got.Message != "egress rules: 2" || got.ObservedGeneration != binding.Generation || !got.LastTransitionTime.Equal(&transitioned) {
		t.Errorf("a changed message moved the transition time, or applied another condition: %+v", got)
	}

	condition = binding.PrepareConditionApply(metav1.Condition{Type: "EgressReady", Status: metav1.ConditionFalse, Reason: PolicySetUnavailable, Message: "missing PolicySets: late"})
	if got := *condition; got.LastTransitionTime.Equal(&transitioned) || got.ObservedGeneration != binding.Generation {
		t.Errorf("a changed status kept the old transition time: %+v", got)
	}

	if !slices.Equal(binding.Status.Conditions, before.Status.Conditions) {
		t.Error("preparing an apply changed the binding it was prepared from")
	}
}

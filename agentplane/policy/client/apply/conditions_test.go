package apply

import (
	"encoding/json"
	"slices"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestPrepareConditionApply(t *testing.T) {
	transitioned := metav1.NewTime(time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC))
	binding := &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: "rules", Name: "grant", Generation: 3},
		Status: policyapi.Status{Conditions: []metav1.Condition{
			{Type: "KubernetesReady", Status: metav1.ConditionTrue, Reason: "Applied", ObservedGeneration: 3, LastTransitionTime: transitioned},
			{Type: "EgressReady", Status: metav1.ConditionTrue, Reason: "Enforced", Message: "egress rules: 1", ObservedGeneration: 3, LastTransitionTime: transitioned},
		}},
	}
	before := binding.DeepCopy()

	if configuration := PrepareConditionApply(binding, metav1.Condition{Type: "EgressReady", Status: metav1.ConditionTrue, Reason: "Enforced", Message: "egress rules: 1"}); configuration != nil {
		t.Errorf("a condition the binding already carries was applied again: %+v", configuration)
	}

	configuration := PrepareConditionApply(binding, metav1.Condition{Type: "EgressReady", Status: metav1.ConditionTrue, Reason: "Enforced", Message: "egress rules: 2"})
	if configuration == nil {
		t.Fatal("changed condition was not returned")
	}
	var got struct {
		Metadata struct {
			Name      string `json:"name"`
			Namespace string `json:"namespace"`
		} `json:"metadata"`
		Status struct {
			ExpiresAt  json.RawMessage    `json:"expiresAt"`
			Conditions []metav1.Condition `json:"conditions"`
		} `json:"status"`
	}
	data, err := json.Marshal(configuration)
	if err != nil {
		t.Fatalf("marshal apply configuration: %v", err)
	}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal apply configuration: %v", err)
	}
	if got.Metadata.Name != binding.Name || got.Metadata.Namespace != binding.Namespace {
		t.Errorf("apply target = %s/%s, want %s/%s", got.Metadata.Namespace, got.Metadata.Name, binding.Namespace, binding.Name)
	}
	if got.Status.ExpiresAt != nil {
		t.Errorf("condition apply unexpectedly owns expiresAt: %s", got.Status.ExpiresAt)
	}
	if len(got.Status.Conditions) != 1 {
		t.Fatalf("condition apply owns %d conditions, want only EgressReady: %+v", len(got.Status.Conditions), got.Status.Conditions)
	}
	condition := got.Status.Conditions[0]
	if condition.Type != "EgressReady" || condition.Message != "egress rules: 2" || condition.ObservedGeneration != binding.Generation || !condition.LastTransitionTime.Equal(&transitioned) {
		t.Errorf("changed message moved the transition time or mapped the condition incorrectly: %+v", condition)
	}

	configuration = PrepareConditionApply(binding, metav1.Condition{Type: "EgressReady", Status: metav1.ConditionFalse, Reason: policyapi.PolicySetUnavailable, Message: "missing PolicySets: late"})
	if configuration == nil {
		t.Fatal("changed status was not returned")
	}
	data, err = json.Marshal(configuration)
	if err != nil {
		t.Fatalf("marshal status transition: %v", err)
	}
	got.Status.Conditions = nil
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal status transition: %v", err)
	}
	if len(got.Status.Conditions) != 1 {
		t.Fatalf("status transition has %d conditions, want one: %+v", len(got.Status.Conditions), got.Status.Conditions)
	}
	condition = got.Status.Conditions[0]
	if condition.Status != metav1.ConditionFalse || condition.LastTransitionTime.Equal(&transitioned) || condition.ObservedGeneration != binding.Generation {
		t.Errorf("changed status kept the old transition time or mapped the condition incorrectly: %+v", condition)
	}

	if !slices.Equal(binding.Status.Conditions, before.Status.Conditions) {
		t.Error("preparing an apply changed the binding it was prepared from")
	}
}

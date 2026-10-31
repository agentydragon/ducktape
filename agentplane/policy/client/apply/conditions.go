// Package apply prepares narrowly owned PolicyBinding server-side apply configurations.
package apply

import (
	"slices"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyapplyv1 "github.com/agentydragon/ducktape/agentplane/policy/client/applyconfigurations/api/v1alpha1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	metav1apply "k8s.io/client-go/applyconfigurations/meta/v1"
)

// PrepareConditionApply returns an apply configuration for a changed condition, or nil when the
// binding already carries it.
func PrepareConditionApply(binding *policyapi.PolicyBinding, condition metav1.Condition) *policyapplyv1.PolicyBindingApplyConfiguration {
	configuration, changed := ConditionApply(binding, condition)
	if !changed {
		return nil
	}
	return configuration
}

// ConditionApply returns an apply configuration holding only this condition, so other readers'
// conditions stay, and whether it differs from the binding's. It calculates transition time from
// the binding's current status. The condition observes the binding's generation.
func ConditionApply(binding *policyapi.PolicyBinding, condition metav1.Condition) (*policyapplyv1.PolicyBindingApplyConfiguration, bool) {
	condition.ObservedGeneration = binding.Generation
	conditions := slices.Clone(binding.Status.Conditions)
	changed := meta.SetStatusCondition(&conditions, condition)
	condition = *meta.FindStatusCondition(conditions, condition.Type)

	return policyapplyv1.PolicyBinding(binding.Name, binding.Namespace).WithStatus(
		policyapplyv1.Status().WithConditions(metav1apply.Condition().
			WithType(condition.Type).
			WithStatus(condition.Status).
			WithObservedGeneration(condition.ObservedGeneration).
			WithLastTransitionTime(condition.LastTransitionTime).
			WithReason(condition.Reason).
			WithMessage(condition.Message)),
	), changed
}

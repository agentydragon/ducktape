package v1alpha1

import (
	"slices"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PolicySetUnavailable is the reason of a condition saying a binding names a PolicySet its reader
// cannot use, so one missing set reads the same in every reader's condition.
const PolicySetUnavailable = "PolicySetUnavailable"

// PrepareConditionApply returns the changed condition for a caller to server-side apply to the
// binding's status, or nil when it already is. Callers apply only this condition so other readers'
// conditions stay. Its observedGeneration is the binding's.
func (b *PolicyBinding) PrepareConditionApply(condition metav1.Condition) *metav1.Condition {
	condition.ObservedGeneration = b.Generation
	conditions := slices.Clone(b.Status.Conditions)
	if !meta.SetStatusCondition(&conditions, condition) {
		return nil
	}
	return meta.FindStatusCondition(conditions, condition.Type)
}

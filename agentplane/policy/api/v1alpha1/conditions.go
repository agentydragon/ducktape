package v1alpha1

import (
	"slices"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PolicySetUnavailable is the reason of a condition saying a binding names a PolicySet its reader
// cannot use, so one missing set reads the same in every reader's condition.
const PolicySetUnavailable = "PolicySetUnavailable"

// PrepareConditionApply returns the object to server-side apply to the binding's status so that
// condition is the binding's condition of its type, or nil when it already is. The object carries
// only that condition, so another reader's conditions stay. Its observedGeneration is the binding's.
func (b *PolicyBinding) PrepareConditionApply(condition metav1.Condition) *PolicyBinding {
	condition.ObservedGeneration = b.Generation
	conditions := slices.Clone(b.Status.Conditions)
	if !meta.SetStatusCondition(&conditions, condition) {
		return nil
	}
	return &PolicyBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: SchemeGroupVersion.String(), Kind: "PolicyBinding"},
		ObjectMeta: metav1.ObjectMeta{Name: b.Name, Namespace: b.Namespace},
		Status:     Status{Conditions: []metav1.Condition{*meta.FindStatusCondition(conditions, condition.Type)}},
	}
}

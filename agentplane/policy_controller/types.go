// Package policycontroller is the Policy Controller: it deletes PolicyBindings past their expiresAt
// and keeps the native RoleBindings and ClusterRoleBindings that their Kubernetes rules expand to.
// Contract: SPEC.md.
//
// +kubebuilder:object:generate=true
// +groupName=agentplane.allegedly.works
package policycontroller

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type RoleRef struct {
	Kind string `json:"kind"`
	Name string `json:"name"`
}

// Rule is one typed rule; this controller reads only the Kubernetes types and skips the rest.
type Rule struct {
	Type        string   `json:"type"`
	Role        *RoleRef `json:"role,omitempty"`
	Namespaces  []string `json:"namespaces,omitempty"`
	ClusterRole string   `json:"clusterRole,omitempty"`
}

type PolicySetSpec struct {
	Rules []Rule `json:"rules"`
}

type Status struct {
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
type PolicySet struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              PolicySetSpec `json:"spec"`
	Status            Status        `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type PolicySetList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []PolicySet `json:"items"`
}

type ServiceAccountRef struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

type PolicyBindingSpec struct {
	Subject    ServiceAccountRef `json:"subject"`
	PolicySets []string          `json:"policySets"`
	ExpiresAt  *metav1.Time      `json:"expiresAt,omitempty"`
}

// +kubebuilder:object:root=true
type PolicyBinding struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              PolicyBindingSpec `json:"spec"`
	Status            Status            `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type PolicyBindingList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []PolicyBinding `json:"items"`
}

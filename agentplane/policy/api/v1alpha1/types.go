// Package v1alpha1 defines Agentplane's v1alpha1 PolicySet and PolicyBinding Kubernetes API.
//
// +kubebuilder:object:generate=true
// +groupName=agentplane.allegedly.works
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type RoleRef struct {
	Kind string `json:"kind"`
	Name string `json:"name"`
}

// Rule is shared by policy consumers; each consumer handles its own Type.
type Rule struct {
	Type            string         `json:"type"`
	Role            *RoleRef       `json:"role,omitempty"`
	Namespaces      []string       `json:"namespaces,omitempty"`
	ClusterRole     string         `json:"clusterRole,omitempty"`
	Hosts           []string       `json:"hosts,omitempty"`
	Methods         []string       `json:"methods,omitempty"`
	Paths           []string       `json:"paths,omitempty"`
	CredentialRef   *CredentialRef `json:"credentialRef,omitempty"`
	ClusterInternal bool           `json:"clusterInternal,omitempty"`
}

type CredentialRef struct {
	Name string `json:"name"`
}

type PolicySetSpec struct {
	Rules []Rule `json:"rules"`
}

type Status struct {
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +genclient
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

// +genclient
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

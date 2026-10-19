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
	// Each reader applies only its own condition type with its own field manager.
	// +listType=map
	// +listMapKey=type
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
	// +kubebuilder:validation:MinLength=1
	Namespace string `json:"namespace"`
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

type PolicyBindingSpec struct {
	// The ServiceAccount this binding grants to. Native bindings also carry its UID so that
	// recreating a same-named account replaces the grant.
	Subject ServiceAccountRef `json:"subject"`
	// PolicySet names in this binding's namespace.
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	PolicySets []string `json:"policySets"`
	// The Policy Controller deletes the binding at this instant. Do not set it on a GitOps-owned
	// binding, which GitOps would recreate.
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty"`
}

// +genclient
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,path=policybindings,singular=policybinding
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Subject",type=string,JSONPath=".spec.subject.name"
// +kubebuilder:printcolumn:name="Expires",type=string,JSONPath=".spec.expiresAt"
// +kubebuilder:printcolumn:name="Kubernetes",type=string,JSONPath=".status.conditions[?(@.type==\"KubernetesReady\")].status"
// +kubebuilder:printcolumn:name="Egress",type=string,JSONPath=".status.conditions[?(@.type==\"EgressReady\")].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
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

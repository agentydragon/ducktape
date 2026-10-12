// Package sandboxcontroller is the Sandbox Controller: it reconciles Agentplane's own Sandbox into
// a state PVC that outlives every incarnation and, while the Sandbox is running, one Pod stamped
// from its SandboxTemplate. Contract: SPEC.md.
//
// +kubebuilder:object:generate=true
// +groupName=agentplane.allegedly.works
package sandboxcontroller

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var (
	GroupVersion  = schema.GroupVersion{Group: "agentplane.allegedly.works", Version: "v1alpha1"}
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}
	AddToScheme   = SchemeBuilder.AddToScheme
)

func init() {
	SchemeBuilder.Register(&Sandbox{}, &SandboxList{}, &SandboxTemplate{}, &SandboxTemplateList{})
}

// StateVolume is the Pod volume the controller fills with the Sandbox's state PVC; the template's
// containers mount it by name.
type StateVolume struct {
	Name  string                           `json:"name"`
	Claim corev1.PersistentVolumeClaimSpec `json:"claim"`
}

type SandboxTemplateSpec struct {
	Pod         corev1.PodTemplateSpec `json:"pod"`
	StateVolume StateVolume            `json:"stateVolume"`
	// StateSchemaVersion is the layout of the state PVC this template's runner reads and writes. A
	// state PVC records the version of the template that created it, and no Pod starts from a
	// template declaring another.
	StateSchemaVersion int32 `json:"stateSchemaVersion"`
}

// +kubebuilder:object:root=true
type SandboxTemplate struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              SandboxTemplateSpec `json:"spec"`
}

// +kubebuilder:object:root=true
type SandboxTemplateList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []SandboxTemplate `json:"items"`
}

type SandboxSpec struct {
	// Template names a SandboxTemplate in the Sandbox's namespace, read again at every start.
	Template string `json:"template"`
	Running  bool   `json:"running"`
	// PolicySets names the PolicySets in the Sandbox's namespace its ServiceAccount is bound to.
	PolicySets []string `json:"policySets,omitempty"`
}

// Incarnation is the Sandbox's current Pod.
type Incarnation struct {
	Name string `json:"name"`
	UID  string `json:"uid"`
}

type SandboxStatus struct {
	Incarnation *Incarnation       `json:"incarnation,omitempty"`
	Conditions  []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
type Sandbox struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              SandboxSpec   `json:"spec"`
	Status            SandboxStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type SandboxList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Sandbox `json:"items"`
}

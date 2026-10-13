// Package sandboxcontroller is the Sandbox Controller: it reconciles Agentplane's own Sandbox into
// a state PVC that outlives every incarnation and, while the Sandbox is running, one Pod stamped
// from its SandboxTemplate. Contract: SPEC.md.
//
// +kubebuilder:object:generate=true
// +groupName=agentplane.allegedly.works
package sandboxcontroller

import (
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	kubevirtv1 "kubevirt.io/api/core/v1"
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

// StateVolume is the volume the controller fills with the Sandbox's state PVC: a Pod's containers
// mount it by name, and a VM guest mounts the virtiofs filesystem of that name.
type StateVolume struct {
	Name  string                           `json:"name"`
	Claim corev1.PersistentVolumeClaimSpec `json:"claim"`
}

// SandboxTemplateSpec sets the Sandbox's kind: exactly one of Pod and VM.
type SandboxTemplateSpec struct {
	Pod *corev1.PodTemplateSpec `json:"pod,omitempty"`
	// VM is the VirtualMachine's spec; the controller sets its run strategy from spec.running.
	VM          *kubevirtv1.VirtualMachineSpec `json:"vm,omitempty"`
	StateVolume StateVolume                    `json:"stateVolume"`
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
	// SessionDefaults are the Runner Gateway's; the controller keeps them only so its updates
	// round-trip them.
	SessionDefaults *SessionDefaults `json:"sessionDefaults,omitempty"`
	// Bootstrap is the shell source the Sandbox's runner runs once, before its first Session.
	Bootstrap string `json:"bootstrap,omitempty"`
}

type SessionDefaults struct {
	Harness         *string `json:"harness,omitempty"`
	Model           *string `json:"model,omitempty"`
	Cwd             *string `json:"cwd,omitempty"`
	ReasoningEffort *string `json:"reasoningEffort,omitempty"`
	Instructions    *string `json:"instructions,omitempty"`
	SetupScript     *string `json:"setupScript,omitempty"`
}

// Incarnation is the Pod the Sandbox's runner currently runs in: its own Pod, or its VM's
// virt-launcher Pod.
type Incarnation struct {
	Name string `json:"name"`
	UID  string `json:"uid"`
}

type SandboxStatus struct {
	Incarnation *Incarnation `json:"incarnation,omitempty"`
	// The UID of the last incarnation whose runner sealed every Session, kept after it stops.
	SealedIncarnation *string `json:"sealedIncarnation,omitempty"`
	// The settled name of SealedIncarnation; readers accept either until writers emit this one.
	QuiescedIncarnation *string            `json:"quiescedIncarnation,omitempty"`
	Conditions          []metav1.Condition `json:"conditions,omitempty"`
}

// sessionsQuiesced reports whether every Session was sealed, under either condition name.
func (s *Sandbox) sessionsQuiesced() bool {
	return meta.IsStatusConditionTrue(s.Status.Conditions, SessionsSealed) || meta.IsStatusConditionTrue(s.Status.Conditions, Quiesced)
}

// quiescedIncarnation reports whether uid is the incarnation whose runner sealed every Session,
// under either field name.
func (s *Sandbox) quiescedIncarnation(uid string) bool {
	return (s.Status.SealedIncarnation != nil && *s.Status.SealedIncarnation == uid) ||
		(s.Status.QuiescedIncarnation != nil && *s.Status.QuiescedIncarnation == uid)
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

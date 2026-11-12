package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const (
	// Kind is the WorkloadCredential kind, for owner references and Certificate labels.
	Kind = "WorkloadCredential"

	// ConditionReady is True once the Certificate for status.credentialID is Ready and its Secret
	// holds the issued key pair, as of status.observedGeneration.
	ConditionReady = "Ready"
)

// IdentitySubject names an Identity in the policy namespace, not in the record's own namespace.
type IdentitySubject struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// UID is the requester's choice to pin the Identity incarnation it resolved: a record naming it
	// fails closed once that Identity is replaced by a same-named one. A record naming none follows
	// the live Identity of its name, as a standing record rendered from git must, since git cannot
	// know a UID (operator, 2026-11-10).
	// +kubebuilder:validation:Format=uuid
	UID types.UID `json:"uid,omitempty"`
}

// Subject is the Identity a workload acts as.
type Subject struct {
	Identity *IdentitySubject `json:"identity"`
}

// Binding is the incarnation an owner accepts: one Pod, or one KubeVirt VMI.
// +kubebuilder:validation:XValidation:rule="has(self.podUID) != has(self.vmiUID)",message="Name exactly one of podUID or vmiUID."
type Binding struct {
	// +kubebuilder:validation:Format=uuid
	PodUID types.UID `json:"podUID,omitempty"`
	// +kubebuilder:validation:Format=uuid
	VMIUID types.UID `json:"vmiUID,omitempty"`
}

// PodRef names one Pod incarnation.
type PodRef struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +kubebuilder:validation:Format=uuid
	UID types.UID `json:"uid"`
}

// WorkloadCredentialSpec is the requester's. A running incarnation is never re-pointed: the subject
// changes only together with a new binding, and a record without one is replaced instead. A binding
// cannot be required at create, because the credential is issued before the Pod or VMI exists.
// +kubebuilder:validation:XValidation:rule="self.subject == oldSelf.subject || (has(self.binding) && (!has(oldSelf.binding) || self.binding != oldSelf.binding))",message="subject changes only with a new binding"
// +kubebuilder:validation:XValidation:rule="!has(self.pod) || (has(self.binding) && (!has(self.binding.podUID) || self.binding.podUID == self.pod.uid))",message="pod names the bound Pod, or a bound VMI's Pod"
// +kubebuilder:validation:XValidation:rule="!has(oldSelf.pod) || !has(self.pod) || self.pod == oldSelf.pod || self.binding != oldSelf.binding",message="pod changes only with a new binding"
type WorkloadCredentialSpec struct {
	Subject Subject `json:"subject"`
	// Binding is absent for a standing workload, and for an owned one before its incarnation exists.
	Binding *Binding `json:"binding,omitempty"`
	// Pod is the Pod the bound incarnation runs in, which a verifier reports as the caller's Pod and
	// checks a forwarded Pod token against: the bound Pod itself, or a bound VMI's virt-launcher Pod,
	// which its owner names once KubeVirt has created it.
	Pod *PodRef `json:"pod,omitempty"`
}

// WorkloadCredentialStatus is written only by the workload-identity component in the Policy
// Controller.
type WorkloadCredentialStatus struct {
	// ObservedGeneration is the spec generation the rest of the status was computed for.
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// CredentialID is the last segment of URI. A credential ID lives exactly as long as the binding
	// it was accepted for; a standing workload's never changes.
	// +kubebuilder:validation:Pattern=`^[0-9a-f]{32}$`
	CredentialID string `json:"credentialID,omitempty"`
	// URI is spiffe://agentplane/<namespace>/<name>/<credentialID>.
	URI string `json:"uri,omitempty"`
	// SecretName is the Secret holding the issued key pair.
	SecretName string `json:"secretName,omitempty"`
	// AcceptedBinding is the first binding the component saw for CredentialID.
	AcceptedBinding *Binding `json:"acceptedBinding,omitempty"`
	// +listType=map
	// +listMapKey=type
	// +kubebuilder:validation:MaxItems=8
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// WorkloadCredential is an owner's request for the SVID one workload runs with. By convention it
// takes its owner's name; its controller owner reference says what the workload is.
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,path=workloadcredentials,singular=workloadcredential
// +kubebuilder:subresource:status
// +kubebuilder:validation:XValidation:rule="size(self.metadata.name) <= 200",message="name must be at most 200 characters, so the Certificate and Secret names derived from it fit"
// +kubebuilder:printcolumn:name="Credential",type=string,JSONPath=".status.credentialID"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type==\"Ready\")].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type WorkloadCredential struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              WorkloadCredentialSpec   `json:"spec"`
	Status            WorkloadCredentialStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type WorkloadCredentialList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []WorkloadCredential `json:"items"`
}

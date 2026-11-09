package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SecretKeyRef names a key of a Secret in the proxy's configured credentials namespace.
type SecretKeyRef struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +kubebuilder:validation:MinLength=1
	Key string `json:"key"`
}

// ProjectedWorkloadTokenSource selects the token the sidecar projected for a destination audience.
type ProjectedWorkloadTokenSource struct {
	// The source names the audience the destination expects. The proxy substitutes only a token the
	// sidecar presented for this audience and it reviewed as the same Pod that authenticated the hop;
	// it never implicitly reuses the hop credential.
	// +kubebuilder:validation:MinLength=1
	Audience string `json:"audience"`
}

// KubernetesServiceAccountSource names a ServiceAccount the proxy acts as by impersonation on the API
// server its own credential reaches: it presents its own bearer and asks the API server to treat the
// request as this account's. No token for the account is minted or held. A named API server is the
// intended extension for remote clusters, not modelled for lack of a use case and additive when one
// appears.
type KubernetesServiceAccountSource struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
	Namespace string `json:"namespace"`
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$`
	Name string `json:"name"`
}

// CredentialSource is exactly one source for the substituted value.
// +kubebuilder:validation:ExactlyOneOf=secretRef;projectedWorkloadToken;kubernetesServiceAccount
type CredentialSource struct {
	SecretRef                *SecretKeyRef                   `json:"secretRef,omitempty"`
	ProjectedWorkloadToken   *ProjectedWorkloadTokenSource   `json:"projectedWorkloadToken,omitempty"`
	KubernetesServiceAccount *KubernetesServiceAccountSource `json:"kubernetesServiceAccount,omitempty"`
}

// TargetMethod is how a target's value is parsed.
// +kubebuilder:validation:Enum=wholeValue;schemeToken;basicUsername;basicPassword;basicWhole;jsonField
type TargetMethod string

const (
	WholeValue    TargetMethod = "wholeValue"
	SchemeToken   TargetMethod = "schemeToken"
	BasicUsername TargetMethod = "basicUsername"
	BasicPassword TargetMethod = "basicPassword"
	BasicWhole    TargetMethod = "basicWhole"
	JSONField     TargetMethod = "jsonField"
)

// Target is one exact location a credential may be presented in. A target fires only where a
// request presents the placeholder in that position, so declaring one does not make it apply to
// traffic that does not use it.
// +kubebuilder:validation:XValidation:rule="self.method == 'schemeToken' ? (has(self.header) && has(self.scheme) && !has(self.field)) : self.method == 'jsonField' ? (has(self.field) && !has(self.header) && !has(self.scheme)) : has(self.header) && !has(self.field) && !has(self.scheme)",message="header, field, and scheme must match method"
type Target struct {
	// How the header value is parsed, and which whole component the placeholder must equal.
	// wholeValue uses the value itself; schemeToken uses `<scheme> <credential>`; basicUsername and
	// basicPassword use the halves of a `Basic base64(username:password)` payload; basicWhole uses the
	// entire Basic payload, with or without a colon; jsonField uses one top-level JSON string field
	// with JSON-safe encoding, without substring replacement or templates.
	Method TargetMethod `json:"method"`
	// Request header the placeholder goes in, matched case-insensitively.
	// +kubebuilder:validation:MinLength=1
	Header string `json:"header,omitempty"`
	// Top-level JSON object field whose entire string value must equal the placeholder (jsonField only).
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	Field string `json:"field,omitempty"`
	// The scheme schemeToken expects, compared case-insensitively. It is meaningful only for schemeToken.
	// +kubebuilder:validation:MinLength=1
	Scheme string `json:"scheme,omitempty"`
}

// +kubebuilder:validation:XValidation:rule="!has(self.source.kubernetesServiceAccount) || (size(self.targets) == 1 && self.targets[0].method == 'schemeToken' && has(self.targets[0].header) && self.targets[0].header.lowerAscii() == 'authorization' && has(self.targets[0].scheme) && self.targets[0].scheme.lowerAscii() == 'bearer')",message="a kubernetesServiceAccount credential has exactly one target: schemeToken Bearer on Authorization"
type EgressCredentialSpec struct {
	// What this credential is and what it can do, in prose, for the sandbox that will present it
	// without ever seeing it. It is served to agents over the rules endpoint, so it must contain
	// nothing secret: name the identity and its reach, never the value.
	// +kubebuilder:validation:MinLength=1
	Description string `json:"description"`
	// Exactly one source for the substituted value. secretRef is resolved in the proxy's configured
	// credentials namespace, never this object's; kubernetesServiceAccount substitutes the proxy's
	// own API server bearer and names the account it impersonates.
	Source CredentialSource `json:"source"`
	// Every exact location this credential may be presented in. A kubernetesServiceAccount credential
	// takes exactly one, schemeToken Bearer on Authorization: it substitutes the proxy's own bearer,
	// which anywhere else, such as a JSON field, could be written into an object.
	// +kubebuilder:validation:MinItems=1
	Targets []Target `json:"targets"`
}

// +genclient
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,path=egresscredentials,singular=egresscredential
// +kubebuilder:printcolumn:name="Description",type=string,JSONPath=".spec.description"
// +kubebuilder:printcolumn:name="Secret",type=string,JSONPath=".spec.source.secretRef.name"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type EgressCredential struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              EgressCredentialSpec `json:"spec"`
}

// +kubebuilder:object:root=true
type EgressCredentialList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []EgressCredential `json:"items"`
}

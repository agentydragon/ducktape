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

// AuthenticatedWorkloadTokenSource selects the bearer retained from the current request's or
// tunnel's successful workload authentication. The proxy never copies an unvalidated
// Proxy-Authorization header, and resolution fails when authenticated context is absent or no
// longer bound.
// +kubebuilder:validation:MaxProperties=0
type AuthenticatedWorkloadTokenSource struct{}

// ProjectedWorkloadTokenSource selects the token the sidecar projected for an audience.
type ProjectedWorkloadTokenSource struct {
	// The hop bearer carries the proxy's own audience, so a destination that validates its own
	// refuses it; this is the same Pod's identity minted for that destination instead. The proxy
	// substitutes it only after reviewing it as the very Pod that authenticated the hop, and only
	// for an audience its own configuration lists.
	// +kubebuilder:validation:MinLength=1
	Audience string `json:"audience"`
}

// CredentialSource is exactly one source for the substituted value.
// +kubebuilder:validation:ExactlyOneOf=secretRef;authenticatedWorkloadToken;projectedWorkloadToken
type CredentialSource struct {
	SecretRef                  *SecretKeyRef                     `json:"secretRef,omitempty"`
	AuthenticatedWorkloadToken *AuthenticatedWorkloadTokenSource `json:"authenticatedWorkloadToken,omitempty"`
	ProjectedWorkloadToken     *ProjectedWorkloadTokenSource     `json:"projectedWorkloadToken,omitempty"`
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

type EgressCredentialSpec struct {
	// What this credential is and what it can do, in prose, for the sandbox that will present it
	// without ever seeing it. It is served to agents over the rules endpoint, so it must contain
	// nothing secret: name the identity and its reach, never the value.
	// +kubebuilder:validation:MinLength=1
	Description string `json:"description"`
	// Exactly one source for the substituted value. secretRef is resolved in the proxy's configured
	// credentials namespace, never this object's.
	Source CredentialSource `json:"source"`
	// Every exact location this credential may be presented in.
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

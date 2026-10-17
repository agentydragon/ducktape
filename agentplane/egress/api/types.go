package api

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SecretKeyRef names a key of a Secret in the proxy's configured credentials namespace.
type SecretKeyRef struct {
	Name string `json:"name"`
	Key  string `json:"key"`
}

// AuthenticatedWorkloadTokenSource selects the bearer retained from the current request's or
// tunnel's successful workload authentication. It has no fields; the CRD admits only `{}`.
type AuthenticatedWorkloadTokenSource struct{}

// ProjectedWorkloadTokenSource selects the token the sidecar projected for an audience.
type ProjectedWorkloadTokenSource struct {
	Audience string `json:"audience"`
}

// CredentialSource is exactly one source for the substituted value.
type CredentialSource struct {
	SecretRef                  *SecretKeyRef                     `json:"secretRef,omitempty"`
	AuthenticatedWorkloadToken *AuthenticatedWorkloadTokenSource `json:"authenticatedWorkloadToken,omitempty"`
	ProjectedWorkloadToken     *ProjectedWorkloadTokenSource     `json:"projectedWorkloadToken,omitempty"`
}

// TargetMethod is how a target's value is parsed.
type TargetMethod string

const (
	WholeValue    TargetMethod = "wholeValue"
	SchemeToken   TargetMethod = "schemeToken"
	BasicUsername TargetMethod = "basicUsername"
	BasicPassword TargetMethod = "basicPassword"
	BasicWhole    TargetMethod = "basicWhole"
	JSONField     TargetMethod = "jsonField"
)

// Target is one exact location a credential may be presented in. Which of Header, Field and Scheme
// it takes depends on Method.
type Target struct {
	Method TargetMethod `json:"method"`
	Header string       `json:"header,omitempty"`
	Field  string       `json:"field,omitempty"`
	Scheme string       `json:"scheme,omitempty"`
}

type EgressCredentialSpec struct {
	Description string           `json:"description"`
	Source      CredentialSource `json:"source"`
	Targets     []Target         `json:"targets"`
}

// +kubebuilder:object:root=true
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

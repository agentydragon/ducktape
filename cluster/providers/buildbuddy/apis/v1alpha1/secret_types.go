/*
Copyright 2025 The Crossplane Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1alpha1

import (
	"reflect"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"

	xpv2 "github.com/crossplane/crossplane/apis/v2/core/v2"
)

// SecretParameters select the value to publish to BuildBuddy.
type SecretParameters struct {
	// ValueSecretRef selects a key from a Secret in this resource's namespace.
	ValueSecretRef xpv2.LocalSecretKeySelector `json:"valueSecretRef"`
}

// SecretObservation are the observable fields of a Secret.
type SecretObservation struct {
	SourceUID             string       `json:"sourceUID,omitempty"`
	SourceResourceVersion string       `json:"sourceResourceVersion,omitempty"`
	SourceKey             string       `json:"sourceKey,omitempty"`
	ExternalName          string       `json:"externalName,omitempty"`
	PublicKeyID           string       `json:"publicKeyID,omitempty"`
	LastAppliedAt         *metav1.Time `json:"lastAppliedAt,omitempty"`
}

// A SecretSpec defines the desired state of a Secret.
type SecretSpec struct {
	xpv2.ManagedResourceSpec `json:",inline"`
	ForProvider              SecretParameters `json:"forProvider"`
}

// A SecretStatus represents the observed state of a Secret.
type SecretStatus struct {
	xpv2.ManagedResourceStatus `json:",inline"`
	AtProvider                 SecretObservation `json:"atProvider,omitempty"`
}

// A Secret manages an organization secret in BuildBuddy.
// +kubebuilder:object:root=true
// +kubebuilder:printcolumn:name="READY",type="string",JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="SYNCED",type="string",JSONPath=".status.conditions[?(@.type=='Synced')].status"
// +kubebuilder:printcolumn:name="EXTERNAL-NAME",type="string",JSONPath=".metadata.annotations.crossplane\\.io/external-name"
// +kubebuilder:printcolumn:name="AGE",type="date",JSONPath=".metadata.creationTimestamp"
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,categories={crossplane,managed,buildbuddy}
type Secret struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   SecretSpec   `json:"spec"`
	Status SecretStatus `json:"status,omitempty"`
}

// SecretList contains a list of Secret
// +kubebuilder:object:root=true
type SecretList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Secret `json:"items"`
}

// Secret type metadata.
var (
	SecretKind             = reflect.TypeOf(Secret{}).Name()
	SecretGroupKind        = schema.GroupKind{Group: Group, Kind: SecretKind}.String()
	SecretKindAPIVersion   = SecretKind + "." + SchemeGroupVersion.String()
	SecretGroupVersionKind = SchemeGroupVersion.WithKind(SecretKind)
)

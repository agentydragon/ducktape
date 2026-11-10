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

// APIKeyCapability is a permission granted to a BuildBuddy API key.
// +kubebuilder:validation:Enum=CACHE_WRITE;REGISTER_EXECUTOR;CAS_WRITE;ORG_ADMIN;AUDIT_LOG_READ;REGISTER_CACHE_PROXY;SEND_NOTIFICATION
type APIKeyCapability string

const (
	APIKeyCapabilityCacheWrite         APIKeyCapability = "CACHE_WRITE"
	APIKeyCapabilityRegisterExecutor   APIKeyCapability = "REGISTER_EXECUTOR"
	APIKeyCapabilityCASWrite           APIKeyCapability = "CAS_WRITE"
	APIKeyCapabilityOrgAdmin           APIKeyCapability = "ORG_ADMIN"
	APIKeyCapabilityAuditLogRead       APIKeyCapability = "AUDIT_LOG_READ"
	APIKeyCapabilityRegisterCacheProxy APIKeyCapability = "REGISTER_CACHE_PROXY"
	APIKeyCapabilitySendNotification   APIKeyCapability = "SEND_NOTIFICATION"
)

// APIKeyParameters specify a group-owned BuildBuddy API key.
type APIKeyParameters struct {
	// GroupID is the BuildBuddy group that owns this key. It cannot be changed
	// after the key is created.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="groupID is immutable"
	GroupID string `json:"groupID"`

	// Label is the human-readable BuildBuddy label for the key.
	// +kubebuilder:validation:MinLength=1
	Label string `json:"label"`

	// Capabilities are the permissions granted to the key.
	// +kubebuilder:validation:UniqueItems=true
	Capabilities []APIKeyCapability `json:"capabilities"`

	// VisibleToDevelopers controls whether BuildBuddy shows the key to
	// non-admin group members. It defaults to false.
	// +kubebuilder:default=false
	VisibleToDevelopers bool `json:"visibleToDevelopers,omitempty"`
}

// APIKeyObservation records non-secret metadata returned by BuildBuddy.
type APIKeyObservation struct {
	GroupID             string             `json:"groupID,omitempty"`
	Label               string             `json:"label,omitempty"`
	Capabilities        []APIKeyCapability `json:"capabilities,omitempty"`
	VisibleToDevelopers bool               `json:"visibleToDevelopers,omitempty"`
}

// An APIKeySpec defines the desired state of an APIKey.
type APIKeySpec struct {
	xpv2.ManagedResourceSpec `json:",inline"`
	ForProvider              APIKeyParameters `json:"forProvider"`
}

// An APIKeyStatus represents the observed state of an APIKey.
type APIKeyStatus struct {
	xpv2.ManagedResourceStatus `json:",inline"`
	AtProvider                 APIKeyObservation `json:"atProvider,omitempty"`
}

// An APIKey manages a group-owned BuildBuddy API key. BuildBuddy returns the
// generated key value only at creation time; the provider publishes it through
// writeConnectionSecretToRef and never stores it in the resource spec or status.
// +kubebuilder:object:root=true
// +kubebuilder:printcolumn:name="READY",type="string",JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="SYNCED",type="string",JSONPath=".status.conditions[?(@.type=='Synced')].status"
// +kubebuilder:printcolumn:name="KEY-ID",type="string",JSONPath=".metadata.annotations.crossplane\\.io/external-name"
// +kubebuilder:printcolumn:name="GROUP",type="string",JSONPath=".status.atProvider.groupID"
// +kubebuilder:printcolumn:name="AGE",type="date",JSONPath=".metadata.creationTimestamp"
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,categories={crossplane,managed,buildbuddy}
type APIKey struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   APIKeySpec   `json:"spec"`
	Status APIKeyStatus `json:"status,omitempty"`
}

// APIKeyList contains a list of APIKey.
// +kubebuilder:object:root=true
type APIKeyList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []APIKey `json:"items"`
}

// APIKey type metadata.
var (
	APIKeyKind             = reflect.TypeOf(APIKey{}).Name()
	APIKeyGroupKind        = schema.GroupKind{Group: Group, Kind: APIKeyKind}.String()
	APIKeyKindAPIVersion   = APIKeyKind + "." + SchemeGroupVersion.String()
	APIKeyGroupVersionKind = SchemeGroupVersion.WithKind(APIKeyKind)
)

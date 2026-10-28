package v1alpha1

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

// Widget is a namespaced fixture resource.
//
// +genclient
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Title",type=string,JSONPath=".spec.title"
type Widget struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   WidgetSpec   `json:"spec,omitempty"`
	Status WidgetStatus `json:"status,omitempty"`
}

// WidgetList is the list response for Widget.
//
// +kubebuilder:object:root=true
type WidgetList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Widget `json:"items"`
}

// WidgetSpec describes the user-provided widget configuration.
type WidgetSpec struct {
	// Title is required and appears as a typed construct property.
	// +kubebuilder:validation:MinLength=1
	Title string `json:"title"`

	// Replicas is bounded to demonstrate marker-to-schema propagation.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=8
	Replicas int32 `json:"replicas"`
}

// WidgetStatus records controller-owned state.
type WidgetStatus struct {
	Ready bool `json:"ready,omitempty"`
}

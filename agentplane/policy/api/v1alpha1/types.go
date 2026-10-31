// Package v1alpha1 defines Agentplane's v1alpha1 PolicySet and PolicyBinding Kubernetes API.
//
// +kubebuilder:object:generate=true
// +groupName=agentplane.allegedly.works
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
)

type RoleRef struct {
	// +kubebuilder:validation:Enum=Role;ClusterRole
	Kind string `json:"kind"`
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// Rule is shared by policy consumers; each consumer handles its own Type.
// +kubebuilder:validation:XValidation:rule="!has(self.hosts) || !self.hosts.exists(h, h == '*') || (!has(self.credentialRef) && (!has(self.clusterInternal) || !self.clusterInternal))",message="A blanket host rule cannot set clusterInternal or credentialRef."
// +kubebuilder:validation:XValidation:rule="self.type == 'kubernetes_role_binding' ? (has(self.role) && has(self.namespaces) && !has(self.clusterRole) && !has(self.hosts) && !has(self.methods) && !has(self.paths) && !has(self.clusterInternal) && !has(self.credentialRef) && !has(self.policy)) : self.type == 'kubernetes_cluster_role_binding' ? (has(self.clusterRole) && !has(self.role) && !has(self.namespaces) && !has(self.hosts) && !has(self.methods) && !has(self.paths) && !has(self.clusterInternal) && !has(self.credentialRef) && !has(self.policy)) : self.type == 'egress' ? (has(self.hosts) && !has(self.role) && !has(self.namespaces) && !has(self.clusterRole) && !has(self.policy)) : self.type == 'session_inbox_access' ? (!has(self.role) && !has(self.namespaces) && !has(self.clusterRole) && !has(self.hosts) && !has(self.methods) && !has(self.paths) && !has(self.clusterInternal) && !has(self.credentialRef) && !has(self.policy)) : has(self.policy) && !has(self.role) && !has(self.namespaces) && !has(self.clusterRole) && !has(self.hosts) && !has(self.methods) && !has(self.paths) && !has(self.clusterInternal) && !has(self.credentialRef)",message="Rule fields must match type."
// +kubebuilder:validation:XValidation:rule="self.type == 'session_inbox_access' ? (has(self.operations) && has(self.targets)) : (!has(self.operations) && !has(self.targets))",message="Rule fields must match type."
type Rule struct {
	// Type selects the consumer that handles this rule.
	// +kubebuilder:validation:Enum=kubernetes_role_binding;kubernetes_cluster_role_binding;egress;action_auto_approve;session_inbox_access
	Type string `json:"type"`
	// Role names the Role or ClusterRole to bind; the named object must already exist.
	Role *RoleRef `json:"role,omitempty"`
	// Namespaces names the target namespaces explicitly, never by selection; labeling a namespace grants nothing.
	// +listType=set
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	Namespaces []string `json:"namespaces,omitempty"`
	// ClusterRole names the ClusterRole bound cluster-wide.
	// +kubebuilder:validation:MinLength=1
	ClusterRole string `json:"clusterRole,omitempty"`
	// Hosts lists exact lowercase hostnames, `*.` subdomains, or `*` for any public destination. Blanket rules do
	// not bypass authentication, bindings, method/path checks or address filtering.
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=256
	// +kubebuilder:validation:items:MaxLength=256
	// +kubebuilder:validation:items:Pattern=`^(\*|(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*)$`
	Hosts []string `json:"hosts,omitempty"`
	// Methods lists admitted HTTP methods; omitted means any method.
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:Enum=GET;HEAD;POST;PUT;PATCH;DELETE;OPTIONS;CONNECT;TRACE
	Methods []string `json:"methods,omitempty"`
	// Paths lists path patterns as the proxy matches them; omitted means any path.
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	Paths []string `json:"paths,omitempty"`
	// CredentialRef names the EgressCredential that this rule lets a subject present, in the same namespace.
	CredentialRef *CredentialRef `json:"credentialRef,omitempty"`
	// ClusterInternal lifts private-address refusal for this rule's hosts only, never for loopback or link-local.
	ClusterInternal bool `json:"clusterInternal,omitempty"`
	// Policy configures the Action evaluator for an action_auto_approve rule.
	Policy *ActionPolicy `json:"policy,omitempty"`
	// Operations lists what a session_inbox_access rule allows on its targets' Session inboxes.
	// +listType=set
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:Enum=send_message
	Operations []string `json:"operations,omitempty"`
	// Targets names the Sandboxes whose Session inboxes a session_inbox_access rule covers.
	Targets *SandboxTargets `json:"targets,omitempty"`
}

// SandboxTargets names Sandboxes in the PolicySet's namespace by name, by inbox tag, or all of them. The fields
// combine additively.
// +kubebuilder:validation:XValidation:rule="has(self.sandboxes) || has(self.inboxTags) || (has(self.allSandboxes) && self.allSandboxes)",message="Name a Sandbox or an inbox tag, or set allSandboxes."
type SandboxTargets struct {
	// Sandboxes names Sandboxes directly.
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:MinItems=1
	Sandboxes []SandboxTarget `json:"sandboxes,omitempty"`
	// AllSandboxes covers every Sandbox in the namespace, including ones created later.
	AllSandboxes bool `json:"allSandboxes,omitempty"`
	// InboxTags covers every Sandbox in the namespace whose `spec.inboxTags` holds one of these when the rule is
	// checked.
	// +listType=set
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	// +kubebuilder:validation:items:MaxLength=63
	InboxTags []string `json:"inboxTags,omitempty"`
}

// SandboxTarget is one Sandbox by name. Without a UID it also covers a deleted and recreated
// Sandbox of that name; with one, only that incarnation.
type SandboxTarget struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// UID pins the target to one incarnation of the Sandbox.
	// +kubebuilder:validation:Format=uuid
	UID types.UID `json:"uid,omitempty"`
}

// CredentialRef names an EgressCredential in the PolicySet's namespace.
type CredentialRef struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// +kubebuilder:validation:MinItems=1
// +kubebuilder:validation:items:MinLength=1
type ActionNames []string

// ActionPolicy selects an Action evaluator and its inputs.
// +kubebuilder:validation:XValidation:rule="self.type == 'exact_actions' ? (!has(self.schema) && !has(self.owner) && !has(self.repository) && !has(self.entities)) : self.type == 'argument_schema' ? (has(self.schema) && !has(self.owner) && !has(self.repository) && !has(self.entities)) : self.type == 'github_repository' ? (!has(self.schema) && has(self.owner) && has(self.repository) && !has(self.entities)) : self.type == 'github_public_repository' ? (!has(self.schema) && !has(self.owner) && !has(self.repository) && !has(self.entities)) : !has(self.schema) && !has(self.owner) && !has(self.repository) && has(self.entities)",message="Action policy fields must match type."
type ActionPolicy struct {
	// Type selects how the evaluator matches an Action request.
	// +kubebuilder:validation:Enum=exact_actions;argument_schema;github_repository;github_public_repository;home_assistant_entity_control
	Type string `json:"type"`
	// Actions lists Action names by ActionGroup key; the policy matches only these.
	// +kubebuilder:validation:MinProperties=1
	Actions map[string]ActionNames `json:"actions"`
	// Schema is a plain JSON Schema over the arguments; properties alone never implies presence, so use required.
	// +kubebuilder:validation:Schemaless
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Type=object
	Schema *runtime.RawExtension `json:"schema,omitempty"`
	// Owner is the GitHub repository owner.
	// +kubebuilder:validation:MinLength=1
	Owner string `json:"owner,omitempty"`
	// Repository is the GitHub repository name.
	// +kubebuilder:validation:MinLength=1
	Repository string `json:"repository,omitempty"`
	// Entities maps Home Assistant entity ids to the services allowed for each.
	// +kubebuilder:validation:MinProperties=1
	Entities map[string]ActionNames `json:"entities,omitempty"`
}

// PolicySetSpec contains the reusable policy rules.
type PolicySetSpec struct {
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=256
	Rules []Rule `json:"rules"`
}

type Status struct {
	// Each reader applies only its own condition type with its own field manager.
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
	// The roles this binding's Kubernetes rules name that do not exist. The Policy Controller writes
	// it with KubernetesReady and binds each role once it appears.
	// +listType=atomic
	MissingRoles []MissingRole `json:"missingRoles,omitempty"`
}

// MissingRole is a role a Kubernetes rule names that does not exist.
type MissingRole struct {
	// The Role's namespace; empty for a ClusterRole.
	Namespace string `json:"namespace,omitempty"`
	// +kubebuilder:validation:Enum=Role;ClusterRole
	Kind string `json:"kind"`
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// +genclient
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,path=policysets,singular=policyset
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type PolicySet struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              PolicySetSpec `json:"spec"`
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

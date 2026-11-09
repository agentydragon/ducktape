// Package v1alpha1 defines Agentplane's v1alpha1 policy, identity and Kubernetes account Kubernetes API.
//
// +kubebuilder:object:generate=true
// +groupName=agentplane.allegedly.works
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
)

// The proto tags on resource fields are stable field numbers for the generated PolicyService
// protobuf schema. Do not reuse or renumber them when changing the Kubernetes API.
type RoleRef struct {
	// +kubebuilder:validation:Enum=Role;ClusterRole
	Kind string `json:"kind" proto:"1"`
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name" proto:"2"`
}

// Rule is shared by policy consumers; each consumer handles its own Type.
// +kubebuilder:validation:XValidation:rule="!has(self.hosts) || !self.hosts.exists(h, h == '*') || (!has(self.credentialRef) && (!has(self.clusterInternal) || !self.clusterInternal))",message="A blanket host rule cannot set clusterInternal or credentialRef."
// +kubebuilder:validation:XValidation:rule="self.type == 'kubernetes_role_binding' ? (has(self.role) && has(self.namespaces) && !has(self.clusterRole) && !has(self.hosts) && !has(self.methods) && !has(self.paths) && !has(self.clusterInternal) && !has(self.credentialRef) && !has(self.policy)) : self.type == 'kubernetes_cluster_role_binding' ? (has(self.clusterRole) && !has(self.role) && !has(self.namespaces) && !has(self.hosts) && !has(self.methods) && !has(self.paths) && !has(self.clusterInternal) && !has(self.credentialRef) && !has(self.policy)) : self.type == 'egress' ? (has(self.hosts) && !has(self.role) && !has(self.namespaces) && !has(self.clusterRole) && !has(self.policy)) : self.type == 'session_inbox_access' ? (!has(self.role) && !has(self.namespaces) && !has(self.clusterRole) && !has(self.hosts) && !has(self.methods) && !has(self.paths) && !has(self.clusterInternal) && !has(self.credentialRef) && !has(self.policy)) : has(self.policy) && !has(self.role) && !has(self.namespaces) && !has(self.clusterRole) && !has(self.hosts) && !has(self.methods) && !has(self.paths) && !has(self.clusterInternal) && !has(self.credentialRef)",message="Rule fields must match type."
// +kubebuilder:validation:XValidation:rule="self.type == 'session_inbox_access' ? (has(self.operations) && has(self.targets)) : (!has(self.operations) && !has(self.targets))",message="Rule fields must match type."
type Rule struct {
	// Type selects the consumer that handles this rule.
	// +kubebuilder:validation:Enum=kubernetes_role_binding;kubernetes_cluster_role_binding;egress;action_auto_approve;session_inbox_access
	Type string `json:"type" proto:"1"`
	// Role names the Role or ClusterRole to bind; the named object must already exist.
	Role *RoleRef `json:"role,omitempty" proto:"2"`
	// Namespaces names the target namespaces explicitly, never by selection; labeling a namespace grants nothing.
	// +listType=set
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	Namespaces []string `json:"namespaces,omitempty" proto:"3"`
	// ClusterRole names the ClusterRole bound cluster-wide.
	// +kubebuilder:validation:MinLength=1
	ClusterRole string `json:"clusterRole,omitempty" proto:"4"`
	// Hosts lists exact lowercase hostnames, `*.` subdomains, or `*` for any public destination. Blanket rules do
	// not bypass authentication, bindings, method/path checks or address filtering.
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=256
	// +kubebuilder:validation:items:MaxLength=256
	// +kubebuilder:validation:items:Pattern=`^(\*|(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*)$`
	Hosts []string `json:"hosts,omitempty" proto:"5"`
	// Methods lists admitted HTTP methods; omitted means any method.
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:Enum=GET;HEAD;POST;PUT;PATCH;DELETE;OPTIONS;CONNECT;TRACE
	Methods []string `json:"methods,omitempty" proto:"6"`
	// Paths lists path patterns as the proxy matches them; omitted means any path.
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	Paths []string `json:"paths,omitempty" proto:"7"`
	// CredentialRef names the EgressCredential that this rule lets a subject present, in the same namespace.
	CredentialRef *CredentialRef `json:"credentialRef,omitempty" proto:"8"`
	// ClusterInternal lifts private-address refusal for this rule's hosts only, never for loopback or link-local.
	ClusterInternal bool `json:"clusterInternal,omitempty" proto:"9"`
	// Policy configures the Action evaluator for an action_auto_approve rule.
	Policy *ActionPolicy `json:"policy,omitempty" proto:"10"`
	// Operations lists what a session_inbox_access rule allows on its targets' Session inboxes.
	// +listType=set
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:Enum=send_message
	Operations []string `json:"operations,omitempty" proto:"11"`
	// Targets names the Sandboxes whose Session inboxes a session_inbox_access rule covers.
	Targets *SandboxTargets `json:"targets,omitempty" proto:"12"`
}

// SandboxTargets names Sandboxes in the PolicySet's namespace by name, by inbox tag, or all of them. The fields
// combine additively.
// +kubebuilder:validation:XValidation:rule="has(self.sandboxes) || has(self.inboxTags) || (has(self.allSandboxes) && self.allSandboxes)",message="Name a Sandbox or an inbox tag, or set allSandboxes."
type SandboxTargets struct {
	// Sandboxes names Sandboxes directly.
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:MinItems=1
	Sandboxes []SandboxTarget `json:"sandboxes,omitempty" proto:"1"`
	// AllSandboxes covers every Sandbox in the namespace, including ones created later.
	AllSandboxes bool `json:"allSandboxes,omitempty" proto:"2"`
	// InboxTags covers every Sandbox in the namespace whose `spec.inboxTags` holds one of these when the rule is
	// checked.
	// +listType=set
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	// +kubebuilder:validation:items:MaxLength=63
	InboxTags []string `json:"inboxTags,omitempty" proto:"3"`
}

// SandboxTarget is one Sandbox by name. Without a UID it also covers a deleted and recreated
// Sandbox of that name; with one, only that incarnation.
type SandboxTarget struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name" proto:"1"`
	// UID pins the target to one incarnation of the Sandbox.
	// +kubebuilder:validation:Format=uuid
	UID types.UID `json:"uid,omitempty" proto:"2"`
}

// CredentialRef names an EgressCredential in the PolicySet's namespace.
type CredentialRef struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name" proto:"1"`
}

// +kubebuilder:validation:MinItems=1
// +kubebuilder:validation:items:MinLength=1
type ActionNames []string

// ActionPolicy selects an Action evaluator and its inputs.
// +kubebuilder:validation:XValidation:rule="self.type == 'exact_actions' ? (!has(self.schema) && !has(self.owner) && !has(self.repository) && !has(self.entities)) : self.type == 'argument_schema' ? (has(self.schema) && !has(self.owner) && !has(self.repository) && !has(self.entities)) : self.type == 'github_repository' ? (!has(self.schema) && has(self.owner) && has(self.repository) && !has(self.entities)) : self.type == 'github_public_repository' ? (!has(self.schema) && !has(self.owner) && !has(self.repository) && !has(self.entities)) : !has(self.schema) && !has(self.owner) && !has(self.repository) && has(self.entities)",message="Action policy fields must match type."
type ActionPolicy struct {
	// Type selects how the evaluator matches an Action request.
	// +kubebuilder:validation:Enum=exact_actions;argument_schema;github_repository;github_public_repository;home_assistant_entity_control
	Type string `json:"type" proto:"1"`
	// Actions lists Action names by ActionGroup key; the policy matches only these.
	// +kubebuilder:validation:MinProperties=1
	Actions map[string]ActionNames `json:"actions" proto:"2"`
	// Schema is a plain JSON Schema over the arguments; properties alone never implies presence, so use required.
	// +kubebuilder:validation:Schemaless
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Type=object
	Schema *runtime.RawExtension `json:"schema,omitempty" proto:"3"`
	// Owner is the GitHub repository owner.
	// +kubebuilder:validation:MinLength=1
	Owner string `json:"owner,omitempty" proto:"4"`
	// Repository is the GitHub repository name.
	// +kubebuilder:validation:MinLength=1
	Repository string `json:"repository,omitempty" proto:"5"`
	// Entities maps Home Assistant entity ids to the services allowed for each.
	// +kubebuilder:validation:MinProperties=1
	Entities map[string]ActionNames `json:"entities,omitempty" proto:"6"`
}

// PolicySetSpec contains the reusable policy rules.
type PolicySetSpec struct {
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=256
	Rules []Rule `json:"rules" proto:"1"`
}

type Status struct {
	// Each reader applies only its own condition type with its own field manager.
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty" proto:"1"`
	// The roles this binding's Kubernetes rules name that do not exist. The Policy Controller writes
	// it with KubernetesReady and binds each role once it appears.
	// +listType=atomic
	MissingRoles []MissingRole `json:"missingRoles,omitempty" proto:"2"`
}

// MissingRole is a role a Kubernetes rule names that does not exist.
type MissingRole struct {
	// The Role's namespace; empty for a ClusterRole.
	Namespace string `json:"namespace,omitempty" proto:"1"`
	// +kubebuilder:validation:Enum=Role;ClusterRole
	Kind string `json:"kind" proto:"2"`
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name" proto:"3"`
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

// IdentityRef names an Identity in the binding's namespace.
type IdentityRef struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name" proto:"1"`
}

// Subject is who a binding grants to: a ServiceAccount by namespace and name, or an Identity. No
// reader resolves an Identity subject yet; each treats such a binding as naming no caller.
// CLEANUP(added 2026-11-08): remove the ServiceAccount variant (namespace, name) in IDENTITY_CUTOVER
// (agentplane/plans/task_dag.md), which rewrites every binding to an Identity subject.
// +kubebuilder:validation:XValidation:rule="has(self.identity) ? !has(self.namespace) && !has(self.name) : has(self.namespace) && has(self.name)",message="Name either a ServiceAccount (namespace and name) or an identity."
type Subject struct {
	// +kubebuilder:validation:MinLength=1
	Namespace string `json:"namespace,omitempty" proto:"1"`
	// +kubebuilder:validation:MinLength=1
	Name     string       `json:"name,omitempty" proto:"2"`
	Identity *IdentityRef `json:"identity,omitempty" proto:"3"`
}

type PolicyBindingSpec struct {
	// The subject this binding grants to. Native bindings also carry a ServiceAccount subject's UID so
	// that recreating a same-named account replaces the grant.
	Subject Subject `json:"subject" proto:"1"`
	// PolicySet names in this binding's namespace.
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	PolicySets []string `json:"policySets" proto:"2"`
	// The Policy Controller deletes the binding at this instant. Do not set it on a GitOps-owned
	// binding, which GitOps would recreate.
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty" proto:"3"`
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

type IdentitySpec struct {
	// Description says who this agent is, for the operator. An identity is a subject only; it holds no harness,
	// model or environment configuration.
	// +kubebuilder:validation:MinLength=1
	Description string `json:"description" proto:"1"`
}

// Identity is who an agent is: the subject of PolicyBindings, independent of any environment it runs in.
// Nothing in Kubernetes owns an identity; its creator deletes it.
// +genclient
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,path=identities,singular=identity
// +kubebuilder:printcolumn:name="Description",type=string,JSONPath=".spec.description"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type Identity struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              IdentitySpec `json:"spec"`
}

// +kubebuilder:object:root=true
type IdentityList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Identity `json:"items"`
}

// KubernetesRule is one native RBAC binding the Policy Controller provisions for a granted account.
// +kubebuilder:validation:XValidation:rule="self.type == 'role_binding' ? (has(self.role) && has(self.namespaces) && !has(self.clusterRole)) : (has(self.clusterRole) && !has(self.role) && !has(self.namespaces))",message="Rule fields must match type."
type KubernetesRule struct {
	// +kubebuilder:validation:Enum=role_binding;cluster_role_binding
	Type string `json:"type" proto:"1"`
	// Role names the Role or ClusterRole to bind in each namespace; the named object must already exist.
	Role *RoleRef `json:"role,omitempty" proto:"2"`
	// Namespaces names the target namespaces explicitly, never by selection; labeling a namespace grants nothing.
	// +listType=set
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	Namespaces []string `json:"namespaces,omitempty" proto:"3"`
	// ClusterRole names the ClusterRole bound cluster-wide.
	// +kubebuilder:validation:MinLength=1
	ClusterRole string `json:"clusterRole,omitempty" proto:"4"`
}

type KubernetesRoleSetSpec struct {
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=256
	Rules []KubernetesRule `json:"rules" proto:"1"`
}

// KubernetesRoleSet is a reusable, named set of Kubernetes RBAC rules that grants attach to ServiceAccounts.
// A set names no apiserver by design: the Policy Controller resolves its roles where each grant's ServiceAccount
// is, reporting any that do not exist there as missing.
// +genclient
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,path=kubernetesrolesets,singular=kubernetesroleset
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type KubernetesRoleSet struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              KubernetesRoleSetSpec `json:"spec"`
}

// +kubebuilder:object:root=true
type KubernetesRoleSetList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []KubernetesRoleSet `json:"items"`
}

// ServiceAccountTarget names a ServiceAccount by namespace and name.
// The account is on the apiserver the egress proxy and the Policy Controller themselves authenticate to: impersonation
// only works there. A named apiserver field is the intended extension for remote clusters, left out because no
// use case needs one yet, and additive when one does.
type ServiceAccountTarget struct {
	// +kubebuilder:validation:MinLength=1
	Namespace string `json:"namespace" proto:"2"`
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name" proto:"3"`
}

type ServiceAccountGrantSpec struct {
	ServiceAccount ServiceAccountTarget `json:"serviceAccount" proto:"1"`
	// RoleSets names KubernetesRoleSets in this grant's namespace. Grants on one ServiceAccount combine additively.
	// +listType=set
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MinLength=1
	RoleSets []string `json:"roleSets" proto:"2"`
	// The Policy Controller deletes the grant at this instant. Do not set it on a GitOps-owned grant, which GitOps
	// would recreate.
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty" proto:"3"`
}

type ServiceAccountGrantStatus struct {
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty" proto:"1"`
	// The roles this grant's role sets name that do not exist; each is bound once it appears.
	// +listType=atomic
	MissingRoles []MissingRole `json:"missingRoles,omitempty" proto:"2"`
}

// ServiceAccountGrant attaches KubernetesRoleSets to one ServiceAccount.
// +genclient
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,path=serviceaccountgrants,singular=serviceaccountgrant
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Namespace",type=string,JSONPath=".spec.serviceAccount.namespace"
// +kubebuilder:printcolumn:name="ServiceAccount",type=string,JSONPath=".spec.serviceAccount.name"
// +kubebuilder:printcolumn:name="Expires",type=string,JSONPath=".spec.expiresAt"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type ServiceAccountGrant struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              ServiceAccountGrantSpec   `json:"spec"`
	Status            ServiceAccountGrantStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type ServiceAccountGrantList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ServiceAccountGrant `json:"items"`
}

// The ServiceAccount is created on the apiserver the egress proxy and the Policy Controller themselves authenticate to: impersonation
// only works there. A named apiserver field is the intended extension for remote clusters, left out because no
// use case needs one yet, and additive when one does.
type ManagedServiceAccountSpec struct {
	// The Policy Controller deletes the account, and with it everything it owns, at this instant.
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty" proto:"2"`
}

// CreatedServiceAccount is the ServiceAccount incarnation the Policy Controller created.
type CreatedServiceAccount struct {
	// +kubebuilder:validation:MinLength=1
	Namespace string `json:"namespace" proto:"1"`
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name" proto:"2"`
	// +kubebuilder:validation:Format=uuid
	UID types.UID `json:"uid" proto:"3"`
}

type ManagedServiceAccountStatus struct {
	ServiceAccount *CreatedServiceAccount `json:"serviceAccount,omitempty" proto:"1"`
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty" proto:"2"`
}

// ManagedServiceAccount is a ServiceAccount Agentplane creates and deletes, holding the expiry and owner the
// ServiceAccount itself cannot carry across namespaces or apiservers.
// +genclient
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,path=managedserviceaccounts,singular=managedserviceaccount
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="ServiceAccount",type=string,JSONPath=".status.serviceAccount.name"
// +kubebuilder:printcolumn:name="Expires",type=string,JSONPath=".spec.expiresAt"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type ManagedServiceAccount struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              ManagedServiceAccountSpec   `json:"spec"`
	Status            ManagedServiceAccountStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type ManagedServiceAccountList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ManagedServiceAccount `json:"items"`
}

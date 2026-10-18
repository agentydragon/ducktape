// Package api defines Agentplane's v1alpha1 EgressCredential Kubernetes API, the credential a
// sandbox presents as a placeholder and the egress proxy substitutes. The CRD manifest under
// agentplane/crds is authored by hand; these types read it.
//
// +kubebuilder:object:generate=true
// +groupName=agentplane.allegedly.works
// +versionName=v1alpha1
package api

package policyaccess

import policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"

// BindingProvenance identifies the authority that owns a binding's source of truth.
type BindingProvenance string

const (
	BindingFromGit      BindingProvenance = "git"
	BindingFromSandbox  BindingProvenance = "sandbox"
	BindingFromOperator BindingProvenance = "operator"
)

func bindingProvenance(binding *policyapi.PolicyBinding) BindingProvenance {
	if _, ok := binding.Labels[policyapi.FluxKustomizationNameLabel]; ok {
		return BindingFromGit
	}
	for _, owner := range binding.OwnerReferences {
		if owner.Controller != nil && *owner.Controller && owner.Kind == "Sandbox" {
			return BindingFromSandbox
		}
	}
	return BindingFromOperator
}

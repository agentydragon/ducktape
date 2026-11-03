package policyaccess

import (
	"unicode/utf8"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

const maxRefusalReasonCharacters = 32_768

// ActionPolicySetView is the Action Service inventory projection returned to the browser.
type ActionPolicySetView struct {
	Name       string  `json:"name"`
	Generation int64   `json:"generation"`
	Refused    *string `json:"refused"`
}

// BindingProvenance identifies the authority that owns a binding's source of truth.
type BindingProvenance string

const (
	BindingFromGit      BindingProvenance = "git"
	BindingFromSandbox  BindingProvenance = "sandbox"
	BindingFromOperator BindingProvenance = "operator"
)

func truncateReason(reason string) string {
	if utf8.RuneCountInString(reason) <= maxRefusalReasonCharacters {
		return reason
	}
	runes := []rune(reason)
	return string(runes[:maxRefusalReasonCharacters])
}

func bindingProvenance(binding *policyapi.PolicyBinding) BindingProvenance {
	if _, ok := binding.Labels[fluxKustomizationLabel]; ok {
		return BindingFromGit
	}
	for _, owner := range binding.OwnerReferences {
		if owner.Controller != nil && *owner.Controller && owner.Kind == "Sandbox" {
			return BindingFromSandbox
		}
	}
	return BindingFromOperator
}

const fluxKustomizationLabel = "kustomize.toolkit.fluxcd.io/name"
